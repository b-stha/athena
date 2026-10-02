import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from athena_ovos import wake
from athena_ovos.setup import DEFAULT_MODEL_SHA256, resolve_model_path


class PreciseWakeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.model = Path(self.directory.name) / "athena.onnx"
        self.model.write_bytes(b"test model")
        self.engine = MagicMock()
        self.engine.hop_samples = 800
        self.engine.buffer_samples = 24000
        self.engine.update.return_value = 0.0
        self.engine_factory = patch("ovos_ww_plugin_precise_onnx.PreciseOnnxEngine",
                                    return_value=self.engine)
        self.engine_factory.start()
        self.addCleanup(self.engine_factory.stop)
        self.detector = self.create_detector()
        self.hop = bytes(1600)
        self.chunk = bytes(640)

    def create_detector(self, **settings):
        return wake.AthenaPreciseWakeWord("athena", {"model": str(self.model), **settings})

    def warmup(self):
        self.detector.update(self.hop * 30)

    def test_twenty_ms_fragments_preserve_samples_and_fifty_ms_hops(self):
        values = [(index % 100) - 50 for index in range(1600)]
        audio = struct.pack("<1600h", *values)
        for offset in range(0, len(audio), 640):
            self.detector.update(audio[offset:offset + 640])
        self.assertEqual(self.engine.update.call_count, 2)
        processed = np.concatenate([call.args[0] for call in self.engine.update.call_args_list])
        np.testing.assert_array_equal(processed, np.array(values, dtype=np.float32) / 32768.0)
        self.assertEqual(processed.dtype, np.float32)
        self.assertFalse(self.detector.found_wake_word())

    def test_incomplete_hop_waits_for_more_audio(self):
        self.detector.update(self.chunk)
        self.detector.update(self.chunk)
        self.engine.update.assert_not_called()
        self.detector.update(self.chunk)
        self.engine.update.assert_called_once()
        self.assertEqual(len(self.detector._pending), 320)

    def test_high_predictions_during_feature_warmup_do_not_trigger(self):
        self.engine.update.return_value = 0.99
        for _ in range(29):
            self.detector.update(self.hop)
            self.assertFalse(self.detector.found_wake_word())
        self.assertEqual(self.detector._detector.activation, 0)

    def test_low_probabilities_are_not_treated_as_truthy_wake_events(self):
        self.engine.update.return_value = 0.01
        for _ in range(70):
            self.detector.update(self.hop)
            self.assertFalse(self.detector.found_wake_word())

    def test_several_above_threshold_predictions_are_required(self):
        self.warmup()
        self.engine.update.return_value = 0.99
        for _ in range(3):
            self.detector.update(self.hop)
            self.assertFalse(self.detector.found_wake_word())
        self.detector.update(self.hop)
        self.assertTrue(self.detector.found_wake_word())

    def test_sensitivity_and_trigger_level_reach_actual_detector(self):
        self.detector = self.create_detector(sensitivity=0.2, trigger_level=1)
        self.assertEqual(self.detector._detector.sensitivity, 0.2)
        self.assertEqual(self.detector._detector.trigger_level, 1)
        self.assertEqual(self.detector._detector.chunk_size, 800)
        self.warmup()
        self.engine.update.return_value = 0.75
        self.detector.update(self.hop * 10)
        self.assertFalse(self.detector.found_wake_word())
        self.engine.update.return_value = 0.9
        self.detector.update(self.hop)
        self.assertFalse(self.detector.found_wake_word())
        self.detector.update(self.hop)
        self.assertTrue(self.detector.found_wake_word())

    def test_detection_remains_latched_until_consumed_once(self):
        self.warmup()
        self.engine.update.return_value = 0.99
        self.detector.update(self.hop * 4)
        self.engine.update.return_value = 0.0
        self.detector.update(self.hop)
        self.assertTrue(self.detector.found_wake_word())
        self.assertFalse(self.detector.found_wake_word())
        self.engine.clear.assert_called_once()
        self.assertEqual(self.detector._received_samples, 0)

    def test_consumed_detection_clears_partial_hop_and_rewarms_features(self):
        self.warmup()
        self.engine.update.return_value = 0.99
        self.detector.update(self.hop * 4 + self.chunk)
        self.assertTrue(self.detector.found_wake_word())
        self.assertEqual(self.detector._pending, bytearray())
        self.assertEqual(self.detector._detector.activation, 0)
        self.detector.update(self.hop * 29)
        self.assertFalse(self.detector.found_wake_word())

    def test_explicit_reset_clears_audio_and_detection_history(self):
        self.detector.update(self.chunk)
        self.detector.reset()
        self.assertEqual(self.detector._pending, bytearray())
        self.assertFalse(self.detector.found_wake_word())
        self.engine.clear.assert_called_once()

    def test_invalid_pcm_is_rejected_before_modifying_audio_history(self):
        with self.assertRaisesRegex(ValueError, "complete 16-bit"):
            self.detector.update(b"\0")
        self.assertEqual(self.detector._pending, bytearray())
        self.engine.update.assert_not_called()

    def test_invalid_detector_configuration_is_rejected(self):
        for config in [{"sensitivity": -0.1}, {"sensitivity": 1.1},
                       {"sensitivity": float("nan")}, {"sensitivity": True},
                       {"trigger_level": -1}, {"trigger_level": 1.5},
                       {"trigger_level": True}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.create_detector(**config)
        with self.assertRaisesRegex(ValueError, "model"):
            wake.AthenaPreciseWakeWord()


class RealAthenaModelTests(unittest.TestCase):
    def test_verified_model_does_not_activate_on_twenty_ms_silence_chunks(self):
        model = resolve_model_path(Path(__file__).resolve().parent.parent)
        if not model.is_file():
            self.skipTest("Run .venv/bin/python -m athena_ovos.setup to test the real Athena model.")
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(), DEFAULT_MODEL_SHA256)
        detector = wake.AthenaPreciseWakeWord("athena", {
            "model": str(model), "sensitivity": 0.5, "trigger_level": 3,
        })
        for _ in range(150):
            detector.update(bytes(640))
            self.assertFalse(detector.found_wake_word())
        self.assertEqual(detector._received_samples, 48000)


if __name__ == "__main__":
    unittest.main()
