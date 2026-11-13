"""Live sample-clock wake threshold and shared PCM timing constants."""

import math

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms: compatible with the existing WebRTC VAD.
FRAME_BYTES = FRAME_SAMPLES * 2


def _seconds(value, name, *, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0)):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}.")
    return value


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
