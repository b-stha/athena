"""Wake-triggered recording through a local Wyoming openWakeWord service."""

import asyncio
import contextlib
import os
import queue
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

import sounddevice as sd
import webrtcvad
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncClient
from wyoming.info import Describe, Info
from wyoming.wake import Detect, Detection

from voice.audio import (CAPTURE_CHANNELS, CHANNELS, FRAME_BYTES, FRAME_MS,
                         FRAME_SAMPLES, INPUT_DEVICE, MAX_SECONDS, SAMPLE_RATE,
                         SAMPLE_WIDTH, source_audio)

START_TIMEOUT_SECONDS = 5
END_SILENCE_SECONDS = 0.8
MIN_SPEECH_SECONDS = 0.1
SERVICE_TIMEOUT_SECONDS = 5
AUDIO_TIMEOUT_SECONDS = 2
PREROLL_MS = 200
FALLBACK_PREROLL_MS = 300
WAKE_TAIL_MS = 200
HISTORY_FRAMES = 2000 // FRAME_MS


class WakeSetupError(RuntimeError):
    """An invalid wake configuration that needs a setup change before retrying."""


@dataclass(frozen=True)
class WakeSettings:
    uri: str
    word: str
    phrase: str

    @classmethod
    def from_environment(cls):
        uri = os.getenv("WAKE_URI", "tcp://127.0.0.1:10400").strip()
        word = os.getenv("WAKE_WORD", "athena").strip()
        phrase = os.getenv("WAKE_PHRASE", word.replace("_", " ")).strip()
        try:
            parsed = urlparse(uri)
            valid = (parsed.scheme == "tcp" and bool(parsed.hostname)
                     and parsed.port is not None and 0 < parsed.port < 65536)
        except ValueError:
            valid = False
        if not valid:
            raise WakeSetupError("WAKE_URI must be a TCP address, such as tcp://127.0.0.1:10400.")
        if not word or not re.search(r"\w", phrase):
            raise WakeSetupError("WAKE_WORD and WAKE_PHRASE must contain a word.")
        return cls(uri=uri, word=word, phrase=phrase)


def strip_wake_phrase(text):
    """Remove a recognized wake phrase only when it begins the transcript."""
    phrase = os.getenv("WAKE_PHRASE", os.getenv("WAKE_WORD", "athena").replace("_", " ")).strip()
    words = re.findall(r"\w+", phrase)
    if not words:
        return text.strip()
    pattern = r"^[\W_]*" + r"[\W_]+".join(re.escape(word) for word in words) + r"\b[\W_]*"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()


def select_preroll(frames, timestamp):
    """Keep the trigger's context and audio received while detection was delayed."""
    frames = list(frames)
    if not frames:
        return []
    cutoff = (timestamp - PREROLL_MS if timestamp is not None
              else frames[-1][0] + FRAME_MS - FALLBACK_PREROLL_MS)
    return [audio for frame_time, audio in frames if frame_time >= cutoff]


class SpeechRecorder:
    """Bound command capture using 20 ms VAD frames, including the wake context."""

    def __init__(self, vad, preroll=(), wake_guard_ms=0):
        self.vad = vad
        self.chunks = []
        self.frames = 0
        self.voiced_frames = 0
        self.silent_frames = 0
        self.done = False
        self.wake_guard_ms = wake_guard_ms
        self.post_guard_voiced_frames = 0
        self.command_started = False
        for frame in preroll:
            if len(frame) != FRAME_BYTES:
                raise RuntimeError("Audio capture returned an incomplete frame; please try again.")
            self.chunks.append(frame)

    @property
    def audio(self):
        if self.voiced_frames * FRAME_MS < MIN_SPEECH_SECONDS * 1000:
            return b""
        return b"".join(self.chunks)

    def process(self, frame):
        if self.done:
            return True
        if len(frame) != FRAME_BYTES:
            raise RuntimeError("Audio capture returned an incomplete frame; please try again.")
        self.chunks.append(frame)
        self.frames += 1
        if self.vad.is_speech(frame, SAMPLE_RATE):
            self.voiced_frames += 1
            self.silent_frames = 0
            if self.frames * FRAME_MS > self.wake_guard_ms:
                self.post_guard_voiced_frames += 1
                self.command_started = (self.post_guard_voiced_frames * FRAME_MS
                                        >= MIN_SPEECH_SECONDS * 1000)
        elif self.voiced_frames:
            self.silent_frames += 1
        usable_speech = self.voiced_frames * FRAME_MS >= MIN_SPEECH_SECONDS * 1000
        elapsed_ms = self.frames * FRAME_MS
        self.done = (elapsed_ms >= MAX_SECONDS * 1000
                     or ((not self.command_started or not usable_speech)
                         and elapsed_ms >= START_TIMEOUT_SECONDS * 1000)
                     or (self.command_started and usable_speech and self.silent_frames * FRAME_MS
                         >= END_SILENCE_SECONDS * 1000))
        return self.done


class _Microphone:
    def __init__(self):
        self.queue = queue.Queue(maxsize=HISTORY_FRAMES)
        self.error = None
        self.stream = None

    def callback(self, data, frames, timing, status):
        if status:
            self.error = "Audio capture failed; please try recording again."
            return
        if frames != FRAME_SAMPLES or len(data) != frames * CAPTURE_CHANNELS * SAMPLE_WIDTH:
            self.error = "Audio capture returned an incomplete frame; please try again."
            return
        try:
            self.queue.put_nowait(source_audio(data))
        except queue.Full:
            self.error = "Audio capture overflowed; please try recording again."

    async def next_frame(self, detection_task=None):
        deadline = asyncio.get_running_loop().time() + AUDIO_TIMEOUT_SECONDS
        while True:
            if self.error:
                raise RuntimeError(self.error)
            if detection_task is not None and detection_task.done():
                return None
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                if not self.stream.active:
                    raise RuntimeError("Microphone stopped while waiting for audio.")
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("Microphone is not producing audio.")
                await asyncio.sleep(FRAME_MS / 2000)


async def _write(client, event):
    await asyncio.wait_for(client.write_event(event), SERVICE_TIMEOUT_SECONDS)


async def _validate_model(client, settings):
    await _write(client, Describe().event())
    event = await asyncio.wait_for(client.read_event(), SERVICE_TIMEOUT_SECONDS)
    if event is None:
        raise RuntimeError("Wake service disconnected before describing its models.")
    if not Info.is_type(event.type):
        raise WakeSetupError("Wake service did not describe its installed models. Check WAKE_URI.")
    try:
        models = {model.name for program in Info.from_event(event).wake
                  for model in program.models
                  if getattr(model, "installed", True) is not False
                  and isinstance(model.name, str)}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise WakeSetupError("Wake service returned invalid model information. Check WAKE_URI.") from error
    if settings.word not in models:
        available = ", ".join(sorted(models)) or "none"
        raise WakeSetupError(f"Wake model '{settings.word}' is not installed (available: {available}). "
                             "Install the custom model and restart the wake service.")


async def _read_detection(client, word):
    while True:
        event = await client.read_event()
        if event is None:
            raise RuntimeError("Wake service disconnected while listening.")
        if event.type == "error":
            raise RuntimeError("Wake service reported a detection error.")
        if Detection.is_type(event.type):
            try:
                detection = Detection.from_event(event)
                if detection.timestamp is not None and not isinstance(detection.timestamp, (int, float)):
                    raise ValueError("Invalid detection timestamp.")
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise RuntimeError("Wake service returned an invalid detection.") from error
            if detection.name in (None, word):
                return detection


async def _record(settings):
    async with AsyncClient.from_uri(settings.uri, connect_timeout=SERVICE_TIMEOUT_SECONDS) as client:
        await _validate_model(client, settings)
        await _write(client, Detect(names=[settings.word]).event())
        await _write(client, AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                       channels=CHANNELS, timestamp=0).event())
        microphone = _Microphone()
        history = deque(maxlen=HISTORY_FRAMES)
        detection_task = asyncio.create_task(_read_detection(client, settings.word))
        try:
            with sd.RawInputStream(device=INPUT_DEVICE, samplerate=SAMPLE_RATE,
                                   channels=CAPTURE_CHANNELS, dtype="int16",
                                   blocksize=FRAME_SAMPLES, callback=microphone.callback) as stream:
                microphone.stream = stream
                print(f"Listening for {settings.phrase.title()} (Ctrl-C to quit).", flush=True)
                timestamp = 0
                while not detection_task.done():
                    frame = await microphone.next_frame(detection_task)
                    if frame is None:
                        break
                    history.append((timestamp, frame))
                    await _write(client, AudioChunk(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                                   channels=CHANNELS, audio=frame,
                                                   timestamp=timestamp).event())
                    timestamp += FRAME_MS
                detection = await detection_task
                print("Wake word detected. Speak your command.", flush=True)
                buffered = list(history)
                preroll = select_preroll(buffered, detection.timestamp)
                if detection.timestamp is None:
                    following = []
                else:
                    following = [frame for frame_time, frame in buffered
                                 if frame_time >= detection.timestamp]
                    preroll = [frame for frame_time, frame in buffered
                               if detection.timestamp - PREROLL_MS <= frame_time
                               < detection.timestamp]
                # A trigger chunk may still contain the end of "Athena". Keep it
                # for Whisper, but give the speaker time to begin their command.
                recorder = SpeechRecorder(webrtcvad.Vad(2), preroll,
                                          wake_guard_ms=WAKE_TAIL_MS)
                for frame in following:
                    if recorder.process(frame):
                        break
                while not recorder.done:
                    recorder.process(await microphone.next_frame())
                if microphone.error:
                    raise RuntimeError(microphone.error)
                return recorder.audio
        finally:
            detection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await detection_task


def record():
    """Wait for the selected wake model, then return one command as mono PCM."""
    settings = WakeSettings.from_environment()
    try:
        return asyncio.run(_record(settings))
    except sd.PortAudioError as error:
        raise RuntimeError(f"Microphone unavailable: {error}") from error
    except TimeoutError as error:
        raise RuntimeError("Wake service timed out. Check the service and WAKE_URI.") from error
    except OSError as error:
        raise RuntimeError("Cannot reach wake service. Check the service and WAKE_URI.") from error
