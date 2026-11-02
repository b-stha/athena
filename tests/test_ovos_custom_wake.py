"""Custom wake integration preserves cadence, gaps and explicit model selection."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main
from athena_ovos.custom_wake import AthenaOpenWakeWord
from athena_ovos.runtime import check_plugins, prepare_environment


class FakeDetector:
    hop_bytes = 2560

    def __init__(self):
        self.pending = bytearray()
        self.parts = []
        self.scores = []
        self.reset_count = 0

    def predict(self, pcm):
        if len(pcm) > self.hop_bytes:
            raise ValueError("oversized model input")
        self.parts.append(pcm)
        self.pending.extend(pcm)
        if len(self.pending) < self.hop_bytes:
            return None
        del self.pending[:self.hop_bytes]
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self):
        self.pending.clear()
        self.reset_count += 1


class CustomWakeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.detector = FakeDetector()
        factory = patch("athena_ovos.custom_wake.OpenWakeWordDetector", return_value=self.detector)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def create(self, **settings):
        return AthenaOpenWakeWord("athena", {"model": "custom.onnx", "melspec_model": "mel.onnx",
            "embedding_model": "embedding.onnx", **settings})

    def test_short_capture_chunks_count_only_fresh_scores(self):
        engine = self.create(consecutive=2, cooldown=0)
        self.detector.scores = [0.99, 0.99]
        for _ in range(7):
            engine.update(bytes(640))
            self.assertFalse(engine.found_wake_word())
        engine.update(bytes(640))
        self.assertTrue(engine.found_wake_word())
        self.assertFalse(engine.found_wake_word())

    def test_large_updates_preserve_samples_and_bound_model_input(self):
        engine = self.create()
        audio = bytes(range(256)) * 23
        engine.update(audio)
        self.assertEqual(b"".join(self.detector.parts), audio)
        self.assertEqual([len(part) for part in self.detector.parts], [2560, 2560, 768])
        self.assertEqual(bytes(self.detector.pending), audio[-768:])

    def test_detection_latches_once_and_consumption_discards_partial_audio(self):
        engine = self.create(cooldown=0)
        self.detector.scores = [0.99]
        engine.update(bytes(2560))
        engine.update(bytes(640))
        self.assertEqual(len(self.detector.pending), 640)
        self.assertTrue(engine.found_wake_word())
        self.assertEqual(self.detector.pending, b"")
        self.assertEqual(self.detector.reset_count, 1)
        self.assertFalse(engine.found_wake_word())
        engine.update(bytes(1920))
        self.assertFalse(engine.found_wake_word())

    def test_listener_reset_drops_features_and_consecutive_evidence(self):
        engine = self.create(consecutive=2, cooldown=0)
        self.detector.scores = [0.99, 0.99, 0.99]
        engine.update(bytes(2560 + 640))
        engine.reset()
        self.assertEqual(self.detector.pending, b"")
        engine.update(bytes(2560))
        self.assertFalse(engine.found_wake_word())
        engine.update(bytes(2560))
        self.assertTrue(engine.found_wake_word())

    def test_cooldown_survives_consumption_and_listener_reset(self):
        engine = self.create(cooldown=0.16)
        self.detector.scores = [0.99, 0.99, 0.99]
        engine.update(bytes(2560))
        self.assertTrue(engine.found_wake_word())
        engine.update(bytes(2560))
        self.assertFalse(engine.found_wake_word())
        engine.reset()
        engine.update(bytes(2560))
        self.assertTrue(engine.found_wake_word())

    def test_invalid_input_preserves_pending_audio_and_stop_prevents_reuse(self):
        engine = self.create()
        engine.update(bytes(640))
        with self.assertRaises(ValueError):
            engine.update(b"x")
        self.assertEqual(len(self.detector.pending), 640)
        engine.stop()
        self.assertEqual(self.detector.pending, b"")
        self.assertFalse(engine.found_wake_word())
        with self.assertRaises(RuntimeError):
            engine.update(bytes(640))

    def test_model_paths_and_settings_reach_the_detector_and_validate(self):
        self.create(seed=11)
        self.factory.assert_called_once_with("custom.onnx", melspec_model="mel.onnx",
                                             embedding_model="embedding.onnx", seed=11)
        for settings in ({"model": None}, {"melspec_model": ""}, {"embedding_model": None},
                         {"threshold": float("nan")}, {"threshold": True},
                         {"consecutive": 0}, {"consecutive": True},
                         {"cooldown": -1}, {"diagnostics": "true"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.create(**settings)


class CustomWakeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = [self.root / name for name in ("hey_athena.onnx", "mel.onnx", "embedding.onnx")]
        for path in self.paths:
            path.write_bytes(b"configuration fixture")
        self.environment = dict(zip(("ATHENA_WAKE_MODEL", "ATHENA_WAKE_MELSPEC_MODEL",
                                     "ATHENA_WAKE_EMBEDDING_MODEL"), map(str, self.paths)))

    def test_custom_paths_and_tuned_settings_are_written_without_precise_settings(self):
        with patch.dict(os.environ, self.environment, clear=True):
            env, config = prepare_environment(self.root, wake_engine="custom")
        hotword = config["hotwords"]["athena"]
        self.assertEqual(hotword["module"], "athena-openwakeword")
        self.assertEqual([hotword[name] for name in ("model", "melspec_model", "embedding_model")],
                         list(map(str, self.paths)))
        self.assertEqual([hotword[name] for name in ("threshold", "consecutive", "cooldown")], [0.98, 1, 2])
        self.assertNotIn("sensitivity", hotword)
        self.assertNotIn("trigger_level", hotword)
        saved = json.loads((Path(env["XDG_CONFIG_HOME"]) / "athena/mycroft.conf").read_text())
        self.assertEqual(saved["hotwords"]["athena"], hotword)

    def test_missing_custom_model_never_uses_the_cached_legacy_model(self):
        legacy = self.root / ".ovos/models/athena.onnx"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy fixture")
        for missing in self.environment:
            environment = {key: value for key, value in self.environment.items() if key != missing}
            with self.subTest(missing=missing), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, missing):
                    prepare_environment(self.root, wake_engine="custom")
                self.assertFalse((self.root / ".ovos/config").exists())

    def test_explicit_classifier_override_and_invalid_assets_fail_before_config_write(self):
        override = self.root / "replacement.onnx"
        override.write_bytes(b"replacement fixture")
        with patch.dict(os.environ, self.environment, clear=True):
            _, config = prepare_environment(self.root, override, wake_engine="custom")
        self.assertEqual(config["hotwords"]["athena"]["model"], str(override))
        for invalid in ("missing.onnx", "not-onnx.txt"):
            candidate = self.root / invalid
            if invalid.endswith(".txt"):
                candidate.write_bytes(b"wrong format")
            with self.subTest(invalid=invalid), patch.dict(os.environ, self.environment, clear=True):
                with self.assertRaisesRegex(ValueError, "ONNX model"):
                    prepare_environment(self.root / invalid, wake_engine="custom", melspec_model=candidate)
                self.assertFalse((self.root / invalid / ".ovos/config").exists())

    def test_plugin_check_only_loads_the_selected_wake_engine(self):
        entries = {}
        def entry_points(*, group):
            names = {"opm.skill": ["athena-skill"], "opm.microphone": ["athena-microphone"],
                "opm.stt": ["athena-wyoming-whisper"], "opm.VAD": ["athena-webrtcvad"],
                "opm.wake_word": ["athena-precise-onnx", "athena-openwakeword"]}[group]
            entries[group] = [MagicMock(name=name) for name in names]
            for entry, name in zip(entries[group], names):
                entry.name = name
            return entries[group]
        with patch("athena_ovos.runtime.importlib.metadata.entry_points", side_effect=entry_points):
            check_plugins("athena-openwakeword")
        precise, custom = entries["opm.wake_word"]
        precise.load.assert_not_called()
        custom.load.assert_called_once()

    def test_application_defaults_to_custom_and_passes_the_selected_plugin(self):
        config = {"hotwords": {"athena": {"module": "athena-openwakeword"}}}
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["main.py"]), \
                patch("main.load_dotenv"), patch("athena_ovos.runtime.prepare_environment", return_value=({}, config)) as prepare, \
                patch("athena_ovos.runtime.check_plugins") as check, \
                patch("athena_ovos.runtime.run", return_value=0), redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(), 0)
        self.assertEqual(prepare.call_args.kwargs["wake_engine"], "custom")
        self.assertEqual(prepare.call_args.kwargs["wake_threshold"], 0.98)
        check.assert_called_once_with("athena-openwakeword")


if __name__ == "__main__":
    unittest.main()
