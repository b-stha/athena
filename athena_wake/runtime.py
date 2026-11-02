"""Sample-clock wake activation and buffered audio handoff.

This module opens no devices and executes no commands. Feed one shared capture
stream as 16 kHz little-endian PCM16 mono. A consumer can start a streaming STT
request with candidate.audio, then send each audio event exactly once. Verification
can complete later while incoming command audio continues to be retained.
"""

from dataclasses import dataclass
from enum import Enum
import math

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms: compatible with the existing WebRTC VAD.
FRAME_BYTES = FRAME_SAMPLES * 2


def _seconds(value, name, *, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0)):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}.")
    return value


class PcmRing:
    """An exact, bounded sample window; snapshot includes the latest frame."""

    def __init__(self, seconds=2.5):
        self.capacity = round(_seconds(seconds, "preroll") * SAMPLE_RATE) * 2
        if not self.capacity:
            raise ValueError("preroll must retain at least one sample.")
        self._audio = bytearray()

    def append(self, pcm):
        if len(pcm) % 2:
            raise ValueError("Audio must contain complete PCM16 samples.")
        self._audio.extend(pcm[-self.capacity:])
        if len(self._audio) > self.capacity:
            del self._audio[:-self.capacity]

    def snapshot(self):
        return bytes(self._audio)

    def clear(self):
        self._audio.clear()


class ThresholdTrigger:
    """Threshold only fresh inference scores; cooldown uses audio time.

    None means the detector has not produced a new prediction. Never count a
    reused prediction from a short microphone chunk as additional evidence.
    """

    def __init__(self, threshold=0.5, consecutive=1, cooldown=2.0):
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold) or not 0 <= threshold <= 1):
            raise ValueError("threshold must be between 0 and 1.")
        if isinstance(consecutive, bool) or not isinstance(consecutive, int) or consecutive < 1:
            raise ValueError("consecutive must be a positive integer.")
        self.threshold = threshold
        self.consecutive = consecutive
        self.cooldown_samples = round(_seconds(cooldown, "cooldown", allow_zero=True) * SAMPLE_RATE)
        self.reset()

    def reset(self):
        self._count = 0
        self._next_sample = 0
        self._last_sample = 0

    def update(self, score, sample):
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < self._last_sample:
            raise ValueError("sample clock must be a nondecreasing integer.")
        self._last_sample = sample
        if score is None:
            return False
        if isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Wake score must be finite and between 0 and 1.")
        if sample < self._next_sample:
            self._count = 0
            return False
        self._count = self._count + 1 if score >= self.threshold else 0
        if self._count < self.consecutive:
            return False
        self._count = 0
        self._next_sample = sample + self.cooldown_samples
        return True


class State(str, Enum):
    IDLE = "idle"
    VERIFYING = "verifying"
    RECORDING = "recording"
    COOLDOWN = "cooldown"
    CLOSED = "closed"


@dataclass(frozen=True)
class WakeEvent:
    kind: str
    sample: int
    audio: bytes = b""
    start_sample: int | None = None
    score: float | None = None
    reason: str | None = None

    @property
    def time(self):
        return self.sample / SAMPLE_RATE


class BufferedWakeSession:
    """Bounded activation lifecycle fed by a single shared capture owner.

    feed() takes exactly one 20 ms frame and a speech decision for that frame.
    Detector.predict returns a new score or None. Detection stays warm during
    recording/cooldown. The caller owns STT and routing; command events contain
    complete audio including preroll, and must have the wake prefix removed
    before any routing. This is a tested handoff primitive, not the OVOS loop.
    """

    def __init__(self, detector, *, preroll=2.5, threshold=0.5, consecutive=1,
                 cooldown=2.0, verify=False, verification_timeout=2.0,
                 speech_timeout=5.0, silence_end=0.8, max_command=30.0):
        if not isinstance(verify, bool):
            raise ValueError("verify must be a boolean.")
        self.detector = detector
        self.ring = PcmRing(preroll)
        self.trigger = ThresholdTrigger(threshold, consecutive, cooldown)
        self.cooldown_samples = self.trigger.cooldown_samples
        self.verify = verify
        self.verification_samples = round(_seconds(verification_timeout, "verification_timeout") * SAMPLE_RATE)
        self.speech_timeout_samples = round(_seconds(speech_timeout, "speech_timeout") * SAMPLE_RATE)
        self.silence_samples = round(_seconds(silence_end, "silence_end") * SAMPLE_RATE)
        self.max_samples = round(_seconds(max_command, "max_command") * SAMPLE_RATE)
        self.state = State.IDLE
        self.sample = 0
        self._audio = bytearray()
        self._speech = False
        self._last_speech = 0
        self._activation = 0
        self._start = 0
        self._cooldown_end = 0

    def _stop(self, kind, reason):
        event = WakeEvent(kind, self.sample, bytes(self._audio) if kind == "command" else b"",
                          self._start, reason=reason)
        self._audio.clear()
        self.ring.clear()  # A subsequent command must not inherit the old one.
        self._speech = False
        self.trigger._count = 0
        self.state = State.COOLDOWN
        self._cooldown_end = self.sample + self.cooldown_samples
        return event

    def feed(self, pcm, *, speech):
        if self.state == State.CLOSED:
            raise RuntimeError("Wake session is closed.")
        if len(pcm) != FRAME_BYTES:
            raise ValueError("feed requires exactly 320 PCM16 mono samples (20 ms).")
        if not isinstance(speech, bool):
            raise ValueError("speech must be a boolean VAD decision.")
        self.sample += FRAME_SAMPLES
        self.ring.append(pcm)
        score = self.detector.predict(pcm)
        events = []
        if self.state == State.COOLDOWN and self.sample >= self._cooldown_end:
            self.state = State.IDLE
            events.append(WakeEvent("idle", self.sample))
        if self.state == State.IDLE:
            if self.trigger.update(score, self.sample):
                self._audio = bytearray(self.ring.snapshot())
                self._start = self.sample - len(self._audio) // 2
                self._activation = self.sample
                self._last_speech = self.sample
                self._speech = False
                self.state = State.VERIFYING if self.verify else State.RECORDING
                events.append(WakeEvent("candidate", self.sample, bytes(self._audio),
                                        self._start, score))
            return events
        if self.state == State.COOLDOWN:
            return events
        self._audio.extend(pcm)
        events.append(WakeEvent("audio", self.sample, pcm, self.sample - FRAME_SAMPLES))
        if speech:
            self._speech = True
            self._last_speech = self.sample
        age = self.sample - self._activation
        if self.state == State.VERIFYING:
            if age >= min(self.verification_samples, self.max_samples):
                events.append(self._stop("rejected", "verification_timeout"))
            return events
        reason = None
        if age >= self.max_samples:
            reason = "recording_timeout"
        elif not self._speech and age >= self.speech_timeout_samples:
            reason = "speech_timeout"
        elif self._speech and self.sample - self._last_speech >= self.silence_samples:
            reason = "silence"
        if reason:
            # Preserve the buffered command even if it finished before a late
            # wake score: lack of post-trigger speech is not proof of no command.
            events.append(self._stop("command", reason))
        return events

    def resolve_verification(self, accepted):
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be a boolean.")
        if self.state != State.VERIFYING:
            raise RuntimeError("No wake candidate is awaiting verification.")
        if not accepted:
            return [self._stop("rejected", "verifier")]
        self.state = State.RECORDING
        return [WakeEvent("verified", self.sample, start_sample=self._start)]

    def close(self):
        """Discard an incomplete recording; never finalize STT on shutdown."""
        if self.state == State.CLOSED:
            return []
        active = self.state in (State.VERIFYING, State.RECORDING)
        self._audio.clear()
        self.ring.clear()
        self.state = State.CLOSED
        return [WakeEvent("cancelled", self.sample, reason="shutdown")] if active else []
