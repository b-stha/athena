"""Runtime checks use real OVOS configuration and harmless child processes."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from athena_ovos.runtime import _check_processes, prepare_environment, stop_processes


class RuntimeConfigurationTests(unittest.TestCase):
    def test_missing_or_empty_model_stops_before_generating_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for model in [root / "missing.onnx", root / "empty.onnx"]:
                if model.name == "empty.onnx":
                    model.touch()
                with self.subTest(model=model.name), self.assertRaisesRegex(ValueError, "model is missing"):
                    prepare_environment(root, model)
                self.assertFalse((root / ".ovos" / "config").exists())

    def test_private_configuration_does_not_overwrite_the_callers_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "test.onnx"
            model.write_bytes(b"configuration-only fixture")
            with patch.dict(os.environ, {"OVOS_CONFIG_BASE_FOLDER": "unrelated",
                                         "XDG_CONFIG_HOME": str(root / "unrelated")}):
                env, _ = prepare_environment(root, model, bus_port=18981)
                self.assertEqual(os.environ["OVOS_CONFIG_BASE_FOLDER"], "unrelated")
                self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(root / "unrelated"))
            private = Path(env["XDG_CONFIG_HOME"]).resolve()
            self.assertTrue(private.is_relative_to(root))
            self.assertEqual(env["OVOS_CONFIG_BASE_FOLDER"], "athena")
            config = json.loads((private / "athena" / "mycroft.conf").read_text())
            self.assertEqual(config["hotwords"]["athena"]["model"], str(model.resolve()))
            self.assertEqual(config["websocket"]["port"], 18981)

    def test_invalid_bus_ports_fail_before_writing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "test.onnx"
            model.write_bytes(b"configuration-only fixture")
            for port in [0, -1, 65536]:
                with self.subTest(port=port), self.assertRaisesRegex(ValueError, "Bus port"):
                    prepare_environment(root, model, bus_port=port)
            self.assertFalse((root / ".ovos" / "config").exists())

    def test_wake_settings_survive_configuration_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "test.onnx"
            model.write_bytes(b"configuration-only fixture")
            env, _ = prepare_environment(root, model, wake_sensitivity=0.2, wake_trigger_level=5)
            path = Path(env["XDG_CONFIG_HOME"]) / "athena" / "mycroft.conf"
            configured = json.loads(path.read_text())["hotwords"]["athena"]
            self.assertEqual((configured["sensitivity"], configured["trigger_level"]), (0.2, 5))
            prepare_environment(root, model)
            defaults = json.loads(path.read_text())["hotwords"]["athena"]
            self.assertEqual((defaults["sensitivity"], defaults["trigger_level"]), (0.5, 3))

    def test_invalid_wake_settings_fail_before_writing_configuration(self):
        invalid = [{"wake_sensitivity": value} for value in
                   (-0.1, 1.1, float("nan"), float("inf"), True, "0.2", None)]
        invalid += [{"wake_trigger_level": value} for value in (-1, 1.5, True, "3", None)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "test.onnx"
            model.write_bytes(b"configuration-only fixture")
            for settings in invalid:
                with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "Wake"):
                    prepare_environment(root, model, **settings)
            self.assertFalse((root / ".ovos" / "config").exists())

    def test_real_merged_config_disables_default_wake_words_and_uses_valid_audio_plugins(self):
        # Import OVOS in a new interpreter because its config singleton caches
        # paths at import time. No model inference, microphone or network calls.
        script = """
import json
from ovos_config import Configuration
from ovos_plugin_manager.microphone import OVOSMicrophoneFactory
from ovos_plugin_manager.stt import OVOSSTTFactory
from ovos_plugin_manager.vad import OVOSVADFactory
from ovos_plugin_manager.wakewords import OVOSWakeWordFactory

config = Configuration()
active = sorted(name for name, values in config["hotwords"].items() if values.get("active", True))
microphone = OVOSMicrophoneFactory.create()
vad = OVOSVADFactory.create()
stt = OVOSSTTFactory.create()
result = {
    "active_hotwords": active,
    "microphone": type(microphone).__name__,
    "format": [microphone.sample_rate, microphone.sample_width, microphone.sample_channels],
    "frame_ms": microphone.frames_per_chunk * 1000 / microphone.sample_rate,
    "silence": bool(vad.is_silence(bytes(microphone.chunk_size))),
    "stt": type(stt).__name__,
    "wake_word": OVOSWakeWordFactory.get_class("athena").__name__,
    "wake_settings": [config["hotwords"]["athena"]["sensitivity"],
                      config["hotwords"]["athena"]["trigger_level"]],
    "pipeline": config["intents"]["pipeline"],
}
print("ATHENA_TEST_RESULT=" + json.dumps(result))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "test.onnx"
            model.write_bytes(b"configuration-only fixture")
            env, _ = prepare_environment(root, model, wake_sensitivity=0.2, wake_trigger_level=5)
            # Also isolate legacy ~/.mycroft and distribution configuration
            # sources so this test cannot depend on another assistant setup.
            env["HOME"] = str(root)
            env["XDG_CONFIG_DIRS"] = str(root / "empty-system-config")
            result = subprocess.run([sys.executable, "-c", script], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.startswith("ATHENA_TEST_RESULT=")]
            self.assertEqual(len(lines), 1, result.stdout)
            actual = json.loads(lines[0].partition("=")[2])
            self.assertEqual(actual["active_hotwords"], ["athena"])
            self.assertEqual(actual["microphone"], "AthenaMicrophone")
            self.assertEqual(actual["format"], [16000, 2, 1])
            self.assertEqual(actual["frame_ms"], 20)
            self.assertTrue(actual["silence"])
            self.assertEqual(actual["stt"], "AthenaWhisperSTT")
            self.assertEqual(actual["wake_word"], "AthenaPreciseWakeWord")
            self.assertEqual(actual["wake_settings"], [0.2, 5])
            self.assertTrue(all(stage.startswith(("adapt_", "fallback_")) for stage in actual["pipeline"]))
            self.assertTrue(actual["pipeline"][-1].startswith("fallback_"))


@unittest.skipUnless(os.name == "posix", "OVOS runtime supervision runs on Linux")
class RuntimeProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.processes = []

    def child(self, *, ignore_interrupt=False):
        marker = self.root / f"child-{len(self.processes)}"
        script = """
from pathlib import Path
import signal
import sys
import time

marker = Path(sys.argv[1])
def stop(signum, frame):
    marker.with_suffix(".stopped").write_text("graceful")
    raise SystemExit(0)
signal.signal(signal.SIGINT, signal.SIG_IGN if sys.argv[2] == "ignore" else stop)
marker.write_text("ready")
while True:
    time.sleep(0.1)
"""
        process = subprocess.Popen([sys.executable, "-u", "-c", script, str(marker),
                                    "ignore" if ignore_interrupt else "graceful"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(process)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 3
        while not marker.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail("Harmless child did not start.")
            time.sleep(0.01)
        return process, marker

    def test_children_receive_graceful_interrupt_and_are_reaped(self):
        bus, bus_marker = self.child()
        listener, listener_marker = self.child()
        stop_processes([bus, listener], timeout=2)
        for process, marker in [(bus, bus_marker), (listener, listener_marker)]:
            self.assertEqual(process.poll(), 0)
            self.assertEqual(marker.with_suffix(".stopped").read_text(), "graceful")

    def test_hung_children_share_one_shutdown_budget_and_are_killed(self):
        bus, _ = self.child(ignore_interrupt=True)
        listener, _ = self.child(ignore_interrupt=True)
        started = time.monotonic()
        stop_processes([bus, listener], timeout=0.25)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8, f"Shutdown exceeded its shared budget: {elapsed}")
        self.assertEqual(bus.poll(), -signal.SIGKILL)
        self.assertEqual(listener.poll(), -signal.SIGKILL)

    def test_already_exited_children_do_not_block_cleanup(self):
        process, marker = self.child()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
        stop_processes([process], timeout=0.1)
        self.assertEqual(process.poll(), 0)
        self.assertTrue(marker.with_suffix(".stopped").exists())

    def test_unexpected_successful_exit_is_still_a_runtime_failure(self):
        process, _ = self.child()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
        with self.assertRaisesRegex(RuntimeError, r"listener exited unexpectedly \(0\)"):
            _check_processes([("listener", process)])


if __name__ == "__main__":
    unittest.main()
