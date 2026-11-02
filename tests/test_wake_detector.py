import hashlib
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

try:
    import numpy as np
except ImportError:
    np = None

from athena_wake.detector import OpenWakeWordDetector


@unittest.skipIf(np is None, "numpy is needed to exercise the model input adapter")
class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = [root / name for name in ("hey_athena.onnx", "mel.onnx", "embedding.onnx")]
        for path in self.paths:
            path.write_bytes(b"model fixture")
        self.engine = MagicMock()
        self.engine.models = {"hey_athena": object()}
        self.engine.predict.return_value = {"hey_athena": .9}
        self.module = ModuleType("openwakeword.model")
        self.module.Model = MagicMock(return_value=self.engine)
        parent = ModuleType("openwakeword")
        self.modules = patch.dict(sys.modules, {"openwakeword": parent, "openwakeword.model": self.module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def create(self, **kwargs):
        return OpenWakeWordDetector(self.paths[0], melspec_model=self.paths[1],
                                    embedding_model=self.paths[2], **kwargs)

    def test_20ms_chunks_produce_only_fresh_80ms_predictions_and_preserve_int16(self):
        detector = self.create()
        audio = struct.pack("<1280h", *range(-640, 640))
        for offset in (0, 640, 1280):
            self.assertIsNone(detector.predict(audio[offset:offset + 640]))
        self.assertEqual(detector.predict(audio[1920:]), .9)
        self.engine.predict.assert_called_once()
        actual = self.engine.predict.call_args.args[0]
        self.assertEqual(actual.dtype, np.dtype("<i2"))
        self.assertEqual(actual.tobytes(), audio)
        options = self.module.Model.call_args.kwargs
        self.assertEqual(options["inference_framework"], "onnx")
        self.assertEqual(options["wakeword_models"], [str(self.paths[0])])
        self.assertEqual(options["melspec_model_path"], str(self.paths[1]))
        self.assertEqual(options["embedding_model_path"], str(self.paths[2]))

    def test_reset_discards_partial_hop_and_invalid_input_preserves_it(self):
        detector = self.create()
        detector.predict(bytes(640))
        for invalid in (b"x", bytes(2562)):
            with self.assertRaises(ValueError):
                detector.predict(invalid)
        self.assertEqual(len(detector._pending), 640)
        detector.reset()
        self.assertEqual(detector._pending, b"")
        self.assertIsNone(detector.predict(bytes(1920)))
        self.assertEqual(detector.predict(bytes(640)), .9)

    def test_initialization_and_reset_do_not_change_external_random_state(self):
        before = np.random.get_state()
        self.module.Model.side_effect = lambda **kwargs: (np.random.rand(), self.engine)[1]
        self.engine.reset.side_effect = lambda: np.random.rand()
        detector = self.create(seed=17)
        detector.reset()
        after = np.random.get_state()
        self.assertEqual(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
        self.assertEqual(before[2:], after[2:])
        metadata = detector.metadata()
        self.assertEqual(metadata["seed"], 17)
        self.assertEqual(metadata["hop_samples"], 1280)
        self.assertEqual(metadata["models"]["wake"]["sha256"], hashlib.sha256(b"model fixture").hexdigest())

    def test_missing_models_seed_and_unexpected_outputs_fail_explicitly(self):
        for seed in (True, -1, 2 ** 32, 1.2):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                self.create(seed=seed)
        detector = self.create()
        for scores in ({"other": .8}, {"hey_athena": float("nan")}, {"hey_athena": 2}):
            with self.subTest(scores=scores):
                self.engine.predict.return_value = scores
                with self.assertRaises(RuntimeError):
                    detector.predict(bytes(2560))
        self.paths[0].unlink()
        with self.assertRaises(ValueError):
            self.create()


if __name__ == "__main__":
    unittest.main()
