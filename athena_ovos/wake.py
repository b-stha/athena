"""Adapt OVOS Precise ONNX to the listener's smaller microphone chunks."""

import math

import numpy as np
from ovos_ww_plugin_precise_onnx import PreciseOnnxHotwordPlugin
from ovos_ww_plugin_precise_onnx.inference import TriggerDetector


class AthenaPreciseWakeWord(PreciseOnnxHotwordPlugin):
    """Preserve Precise's hop cadence and suppress empty feature-window triggers.

    The published OVOS plugin predicts on every supplied chunk. With Athena's
    20 ms chunks, its detector can activate on an unfilled feature window. Feed
    complete 50 ms engine hops and only evaluate triggers after 1.5 seconds of
    real audio have populated the engine's feature history.
    """

    def __init__(self, key_phrase="athena", config=None):
        configuration = dict(config or {})
        try:
            sensitivity = float(configuration.get("sensitivity", 0.5))
        except (TypeError, ValueError) as error:
            raise ValueError("Wake sensitivity must be between 0 and 1.") from error
        if (isinstance(configuration.get("sensitivity"), bool)
                or not math.isfinite(sensitivity) or not 0 <= sensitivity <= 1):
            raise ValueError("Wake sensitivity must be between 0 and 1.")
        trigger_level = configuration.get("trigger_level", 3)
        if isinstance(trigger_level, bool) or not isinstance(trigger_level, int) or trigger_level < 0:
            raise ValueError("Wake trigger_level must be a nonnegative integer.")
        if not configuration.get("model"):
            raise ValueError("Configure Athena's Precise ONNX model before listening.")
        configuration.update(sensitivity=sensitivity, trigger_level=trigger_level)
        super().__init__(key_phrase, configuration)

        self._pending = bytearray()
        self._received_samples = 0
        self._hop_bytes = self.engine.hop_samples * 2
        self._detector = TriggerDetector(
            chunk_size=self.engine.hop_samples,
            sensitivity=sensitivity, trigger_level=trigger_level,
        )
        self.engine.trigger_detector = self._detector

    def update(self, chunk):
        if len(chunk) % 2:
            raise ValueError("Wake audio must contain complete 16-bit samples.")
        self._pending.extend(chunk)
        while len(self._pending) >= self._hop_bytes:
            hop = bytes(self._pending[:self._hop_bytes])
            del self._pending[:self._hop_bytes]
            audio = np.frombuffer(hop, dtype="<i2").astype(np.float32) / 32768.0
            probability = self.engine.update(audio)
            self._received_samples += len(audio)
            if self._received_samples >= self.engine.buffer_samples and not self.trigger_flag:
                if self._detector.update(probability):
                    self.trigger_flag = True

    def reset(self):
        self.trigger_flag = False
        self._pending.clear()
        self._received_samples = 0
        self.engine.clear()
        # A fresh 1.5-second feature warmup exceeds the detector's refractory
        # period, so discard the previous activation counter along with audio.
        self._detector.activation = 0

    def found_wake_word(self):
        if not self.trigger_flag:
            return False
        self.reset()
        return True
