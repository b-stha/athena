"""Explicit ONNX openWakeWord adapter; no automatic model downloads."""

import hashlib
from pathlib import Path


def model_file(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".onnx" or not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Expected an existing nonempty ONNX model: {path}")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OpenWakeWordDetector:
    """Accumulate short capture frames into fresh 80 ms model predictions.

    Use a new instance per recording. v0.6.0 initializes features with random
    noise, so initialization and reset use a recorded seed without changing the
    caller's numpy RNG state. Construction/reset belongs on the replay thread.
    """

    hop_bytes = 1280 * 2

    def __init__(self, model, *, melspec_model, embedding_model, seed=0):
        self.paths = {"wake": model_file(model), "melspec": model_file(melspec_model),
                      "embedding": model_file(embedding_model)}
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 32:
            raise ValueError("seed must be an integer from 0 to 2**32-1.")
        self.seed = seed
        import numpy as np
        from openwakeword.model import Model
        self.np = np
        state = np.random.get_state()
        try:
            np.random.seed(seed)
            self.engine = Model(
                wakeword_models=[str(self.paths["wake"])], inference_framework="onnx",
                melspec_model_path=str(self.paths["melspec"]),
                embedding_model_path=str(self.paths["embedding"]),
            )
        finally:
            np.random.set_state(state)
        self.key = self.paths["wake"].stem
        if self.key not in self.engine.models:
            raise ValueError(f"Wake model did not expose expected score key {self.key!r}.")
        self._pending = bytearray()
        self.reset()

    def reset(self):
        state = self.np.random.get_state()
        try:
            self.np.random.seed(self.seed)
            self.engine.reset()
        finally:
            self.np.random.set_state(state)
        self._pending.clear()

    def predict(self, pcm):
        if len(pcm) % 2 or len(pcm) > self.hop_bytes:
            raise ValueError("Supply complete PCM16 mono samples in chunks of at most 80 ms.")
        self._pending.extend(pcm)
        if len(self._pending) < self.hop_bytes:
            return None
        audio = self.np.frombuffer(bytes(self._pending[:self.hop_bytes]), dtype="<i2")
        del self._pending[:self.hop_bytes]
        scores = self.engine.predict(audio)
        if self.key not in scores:
            raise RuntimeError(f"Wake model returned no {self.key!r} score.")
        score = float(scores[self.key])
        if not self.np.isfinite(score) or not 0 <= score <= 1:
            raise RuntimeError("Wake model returned an invalid probability.")
        return score

    def metadata(self):
        return {"framework": "onnx", "seed": self.seed, "hop_samples": self.hop_bytes // 2,
                "models": {name: {"path": str(path), "sha256": sha256(path)}
                           for name, path in self.paths.items()}}
