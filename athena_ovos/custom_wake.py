"""Run the trained Athena openWakeWord detector inside the OVOS audio loop."""

from ovos_plugin_manager.templates.hotwords import HotWordEngine
from ovos_utils.log import LOG

from athena_wake.detector import OpenWakeWordDetector
from athena_wake.runtime import ThresholdTrigger


class AthenaOpenWakeWord(HotWordEngine):
    """Consume fresh scores and clear features across command-recording gaps.

    OVOS owns capture and stops feeding listen-word engines while recording a
    command. A consumed detection and the subsequent listener reset therefore
    discard model features and partial hops. Cooldown survives those resets and
    is measured conservatively in audio samples actually fed to this engine.
    """

    def __init__(self, key_phrase="athena", config=None):
        super().__init__(key_phrase, config)
        settings = self.config
        for name in ("model", "melspec_model", "embedding_model"):
            if not settings.get(name):
                raise ValueError(f"Configure the custom Athena {name} before listening.")
        self.trigger = ThresholdTrigger(
            threshold=settings.get("threshold", 0.98),
            consecutive=settings.get("consecutive", 1),
            cooldown=settings.get("cooldown", 2.0),
        )
        self.diagnostics = settings.get("diagnostics", False)
        if not isinstance(self.diagnostics, bool):
            raise ValueError("Wake diagnostics must be a boolean.")
        self.detector = OpenWakeWordDetector(
            settings["model"], melspec_model=settings["melspec_model"],
            embedding_model=settings["embedding_model"], seed=settings.get("seed", 0),
        )
        self._sample = 0
        self._resume_sample = 0
        self._triggered = False
        self._closed = False
        self._next_report = 16000
        self._max_score = 0.0

    def update(self, chunk):
        if self._closed:
            raise RuntimeError("The custom Athena wake detector is stopped.")
        if len(chunk) % 2:
            raise ValueError("Wake audio must contain complete PCM16 samples.")
        for offset in range(0, len(chunk), self.detector.hop_bytes):
            part = chunk[offset:offset + self.detector.hop_bytes]
            self._sample += len(part) // 2
            score = self.detector.predict(part)
            if score is not None:
                self._max_score = max(self._max_score, score)
            if not self._triggered and self._sample >= self._resume_sample:
                if self.trigger.update(score, self._sample):
                    self._triggered = True
                    self._resume_sample = self._sample + self.trigger.cooldown_samples
            if self.diagnostics and self._sample >= self._next_report:
                LOG.info(f"Custom wake diagnostic: max_score={self._max_score:.4f} "
                         f"threshold={self.trigger.threshold:.4f} triggered={self._triggered}")
                self._max_score = 0.0
                self._next_report = self._sample + 16000

    def reset(self):
        self._triggered = False
        self.detector.reset()
        self.trigger.reset()
        self._max_score = 0.0

    def found_wake_word(self):
        if not self._triggered:
            return False
        self.reset()
        return True

    def stop(self):
        self.reset()
        self._closed = True
