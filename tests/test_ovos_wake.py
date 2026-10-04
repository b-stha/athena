import hashlib
import math
import re
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
        self.engine.sample_rate = 16000
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

    @staticmethod
    def diagnostic_reports(logger):
        return [dict(re.findall(r"([a-z_]+)=([^\s]+)", call.args[0]))
                for call in logger.call_args_list
                if call.args and call.args[0].startswith("Wake diagnostic: ")]

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
                       {"trigger_level": True}, {"diagnostics": 1},
                       {"diagnostics": "true"}, {"diagnostics": None}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.create_detector(**config)
        with self.assertRaisesRegex(ValueError, "model"):
            wake.AthenaPreciseWakeWord()

    def test_diagnostics_preserve_model_audio_and_triggers_under_fragmented_pcm(self):
        audio = ((np.arange(64000) % 8000) - 4000).astype("<i2").tobytes()
        fragments = [640, 320, 960, 160, 1440]
        outcomes = []
        for enabled in (False, True):
            self.engine.update.reset_mock()
            self.engine.update.side_effect = [0.05] * 40 + [0.95] * 5 + [0.05] * 30 + [0.95] * 5
            with patch.object(wake.LOG, "info") as logger:
                detector = self.create_detector(diagnostics=enabled)
                found = []
                offset = 0
                fragment_index = 0
                while offset < len(audio):
                    length = fragments[fragment_index % len(fragments)]
                    detector.update(audio[offset:offset + length])
                    found.append(detector.found_wake_word())
                    offset += length
                    fragment_index += 1
                processed = [call.args[0].copy() for call in self.engine.update.call_args_list]
                reports = self.diagnostic_reports(logger)
            outcomes.append((processed, found))
            self.assertEqual(bool(reports), enabled)
        self.assertEqual(outcomes[0][1], outcomes[1][1])
        self.assertTrue(any(outcomes[0][1]), "Fixture should exercise an actual trigger.")
        self.assertEqual(len(outcomes[0][0]), len(outcomes[1][0]))
        for normal, diagnosed in zip(outcomes[0][0], outcomes[1][0]):
            np.testing.assert_array_equal(normal, diagnosed)

    def test_diagnostics_wait_for_warmup_then_report_input_seconds_and_interval_maximum(self):
        with patch.object(wake.LOG, "info") as logger:
            detector = self.create_detector(diagnostics=True, sensitivity=0.2, trigger_level=5)
            self.engine.update.return_value = 0.99
            detector.update(self.hop * 29)
            self.assertEqual(self.diagnostic_reports(logger), [])
            self.engine.update.return_value = 0.2
            detector.update(self.hop * 10)
            self.engine.update.return_value = 0.7
            detector.update(self.hop)
            self.engine.update.return_value = 0.2
            detector.update(self.hop * 9)
            self.assertEqual(self.diagnostic_reports(logger), [])
            detector.update(self.hop)
            reports = self.diagnostic_reports(logger)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["triggered"], "no")
            self.assertAlmostEqual(float(reports[0]["score"]), 0.2)
            self.assertAlmostEqual(float(reports[0]["max_score"]), 0.7)
            self.assertEqual(int(reports[0]["frames"]), 21)
            self.assertEqual(int(reports[0]["above_threshold"]), 0)
            detector.update(self.hop * 19)
            self.assertEqual(len(self.diagnostic_reports(logger)), 1)
            detector.update(self.hop)
            reports = self.diagnostic_reports(logger)
            self.assertEqual(len(reports), 2)
            self.assertEqual(int(reports[1]["frames"]), 20)
            self.assertAlmostEqual(float(reports[1]["max_score"]), 0.2)

    def test_diagnostics_report_trigger_counter_immediately_and_without_duplicate_periodic_row(self):
        for trigger_hop in (36, 50):
            with self.subTest(trigger_hop=trigger_hop), patch.object(wake.LOG, "info") as logger:
                detector = self.create_detector(diagnostics=True, sensitivity=0.2, trigger_level=5)
                self.engine.update.return_value = 0.1
                detector.update(self.hop * (trigger_hop - 6))
                self.engine.update.return_value = 0.9
                detector.update(self.hop * 5)
                self.assertEqual(self.diagnostic_reports(logger), [])
                detector.update(self.hop)
                reports = self.diagnostic_reports(logger)
                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(report["triggered"], "yes")
                self.assertAlmostEqual(float(report["score"]), 0.9)
                self.assertAlmostEqual(float(report["max_score"]), 0.9)
                self.assertAlmostEqual(float(report["threshold"]), 0.8)
                self.assertEqual(int(report["activation"]), 6)
                self.assertEqual(int(report["max_activation"]), 6)
                self.assertEqual(int(report["trigger_level"]), 5)
                self.assertEqual(int(report["above_threshold"]), 6)
                self.assertTrue(detector.found_wake_word())

    def test_diagnostics_measure_rolling_levels_and_expire_an_old_loud_hop(self):
        quiet = struct.pack("<800h", *([8192] * 800))
        loud = struct.pack("<800h", *([16384] * 800))
        with patch.object(wake.LOG, "info") as logger:
            detector = self.create_detector(diagnostics=True)
            detector.update(quiet * 20 + loud + quiet * 29)
            first = self.diagnostic_reports(logger)[0]
            expected_rms = 20 * math.log10(math.sqrt((0.5 ** 2 + 29 * 0.25 ** 2) / 30))
            self.assertAlmostEqual(float(first["rms_dbfs"]), expected_rms, delta=0.02)
            self.assertAlmostEqual(float(first["peak_dbfs"]), 20 * math.log10(0.5), delta=0.02)
            self.assertEqual(int(first["window_ms"]), 1500)
            detector.update(quiet * 20)
            reports = self.diagnostic_reports(logger)
            self.assertEqual(len(reports), 2)
            expected_quiet = 20 * math.log10(0.25)
            self.assertAlmostEqual(float(reports[1]["rms_dbfs"]), expected_quiet, delta=0.02)
            self.assertAlmostEqual(float(reports[1]["peak_dbfs"]), expected_quiet, delta=0.02)

    def test_diagnostics_reset_restarts_cadence_and_defines_silent_levels(self):
        loud = struct.pack("<800h", *([16384] * 800))
        with patch.object(wake.LOG, "info") as logger:
            detector = self.create_detector(diagnostics=True, sensitivity=0.2, trigger_level=5)
            self.engine.update.return_value = 0.7
            detector.update(loud * 55)
            self.assertEqual(len(self.diagnostic_reports(logger)), 1)
            detector.reset()
            self.engine.update.return_value = 0.0
            detector.update(self.hop * 49)
            self.assertEqual(len(self.diagnostic_reports(logger)), 1)
            detector.update(self.hop)
            reports = self.diagnostic_reports(logger)
            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[1]["triggered"], "no")
            self.assertEqual(float(reports[1]["max_score"]), 0.0)
            self.assertEqual(int(reports[1]["activation"]), 0)
            self.assertEqual(int(reports[1]["max_activation"]), 0)
            self.assertEqual(int(reports[1]["frames"]), 21)
            self.assertEqual(float(reports[1]["rms_dbfs"]), -math.inf)
            self.assertEqual(float(reports[1]["peak_dbfs"]), -math.inf)


class RealAthenaModelTests(unittest.TestCase):
    def test_verified_model_does_not_activate_on_twenty_ms_silence_chunks(self):
        model = resolve_model_path(Path(__file__).resolve().parent.parent)
        if not model.is_file():
            self.skipTest("Run .venv/bin/python -m athena_ovos.setup to test the real Athena model.")
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(), DEFAULT_MODEL_SHA256)
        for enabled in (False, True):
            with self.subTest(diagnostics=enabled), patch.object(wake.LOG, "info") as logger:
                detector = wake.AthenaPreciseWakeWord("athena", {
                    "model": str(model), "sensitivity": 0.5, "trigger_level": 3,
                    "diagnostics": enabled,
                })
                for _ in range(150):
                    detector.update(bytes(640))
                    self.assertFalse(detector.found_wake_word())
                self.assertEqual(detector._received_samples, 48000)
                reports = PreciseWakeTests.diagnostic_reports(logger)
                self.assertEqual(bool(reports), enabled)
                if enabled:
                    self.assertEqual(float(reports[0]["rms_dbfs"]), -math.inf)
                    self.assertEqual(float(reports[0]["peak_dbfs"]), -math.inf)


if __name__ == "__main__":
    unittest.main()
