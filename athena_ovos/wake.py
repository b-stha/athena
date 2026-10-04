"""Adapt OVOS Precise ONNX to the listener's smaller microphone chunks."""

from collections import deque
import math

import numpy as np
from ovos_utils.log import LOG
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
        diagnostics = configuration.get("diagnostics", False)
        if not isinstance(diagnostics, bool):
            raise ValueError("Wake diagnostics must be a boolean.")
        if not configuration.get("model"):
            raise ValueError("Configure Athena's Precise ONNX model before listening.")
        configuration.update(sensitivity=sensitivity, trigger_level=trigger_level)
        super().__init__(key_phrase, configuration)

        self.diagnostics = diagnostics
        self._pending = bytearray()
        self._received_samples = 0
        self._hop_bytes = self.engine.hop_samples * 2
        self._detector = TriggerDetector(
            chunk_size=self.engine.hop_samples,
            sensitivity=sensitivity, trigger_level=trigger_level,
        )
        self.engine.trigger_detector = self._detector
        # Keep scalar summaries of the model's audio history, never recordings.
        self._diagnostic_audio = deque(maxlen=math.ceil(
            self.engine.buffer_samples / self.engine.hop_samples)) if diagnostics else None
        if diagnostics:
            self._reset_diagnostics()

    def _reset_diagnostics(self):
        self._diagnostic_audio.clear()
        self._diagnostic_next_report = self.engine.buffer_samples + self.engine.sample_rate
        self._diagnostic_frames = 0
        self._diagnostic_above_threshold = 0
        self._diagnostic_max_score = 0.0
        self._diagnostic_max_activation = 0

    def _update_diagnostics(self, score, activation, triggered):
        threshold = 1.0 - self._detector.sensitivity
        self._diagnostic_frames += 1
        self._diagnostic_above_threshold += int(score > threshold)
        self._diagnostic_max_score = max(self._diagnostic_max_score, score)
        self._diagnostic_max_activation = max(self._diagnostic_max_activation, activation)
        if not triggered and self._received_samples < self._diagnostic_next_report:
            return
        samples = sum(item[1] for item in self._diagnostic_audio)
        energy = sum(item[0] for item in self._diagnostic_audio)
        peak = max(item[2] for item in self._diagnostic_audio)
        rms_dbfs = 10 * math.log10(energy / samples) if energy else -math.inf
        peak_dbfs = 20 * math.log10(peak) if peak else -math.inf
        LOG.info(
            f"Wake diagnostic: triggered={'yes' if triggered else 'no'} "
            f"score={score:.4f} max_score={self._diagnostic_max_score:.4f} "
            f"threshold={threshold:.4f} activation={activation} "
            f"max_activation={self._diagnostic_max_activation} "
            f"trigger_level={self._detector.trigger_level} "
            f"frames={self._diagnostic_frames} above_threshold={self._diagnostic_above_threshold} "
            f"rms_dbfs={rms_dbfs:.2f} peak_dbfs={peak_dbfs:.2f} "
            f"window_ms={samples * 1000 / self.engine.sample_rate:.0f}"
        )
        self._diagnostic_next_report = self._received_samples + self.engine.sample_rate
        self._diagnostic_frames = 0
        self._diagnostic_above_threshold = 0
        self._diagnostic_max_score = 0.0
        self._diagnostic_max_activation = 0

    def update(self, chunk):
        if len(chunk) % 2:
            raise ValueError("Wake audio must contain complete 16-bit samples.")
        self._pending.extend(chunk)
        while len(self._pending) >= self._hop_bytes:
            hop = bytes(self._pending[:self._hop_bytes])
            del self._pending[:self._hop_bytes]
            audio = np.frombuffer(hop, dtype="<i2").astype(np.float32) / 32768.0
            if self.diagnostics:
                self._diagnostic_audio.append((float(np.dot(audio, audio)), len(audio),
                                               float(np.max(np.abs(audio)))))
            probability = self.engine.update(audio)
            self._received_samples += len(audio)
            if self._received_samples >= self.engine.buffer_samples and not self.trigger_flag:
                activation_before = self._detector.activation
                triggered = self._detector.update(probability)
                if triggered:
                    self.trigger_flag = True
                if self.diagnostics:
                    # A trigger replaces the native counter with its negative
                    # cooldown value. Report the count that caused activation.
                    activation = activation_before + 1 if triggered else self._detector.activation
                    self._update_diagnostics(probability, activation, triggered)

    def reset(self):
        self.trigger_flag = False
        self._pending.clear()
        self._received_samples = 0
        self.engine.clear()
        # A fresh 1.5-second feature warmup exceeds the detector's refractory
        # period, so discard the previous activation counter along with audio.
        self._detector.activation = 0
        if self.diagnostics:
            self._reset_diagnostics()

    def found_wake_word(self):
        if not self.trigger_flag:
            return False
        self.reset()
        return True
