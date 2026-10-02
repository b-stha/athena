import unittest
import io
import math
import struct
import tempfile
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, MagicMock, patch

from wyoming.asr import Transcript

from voice import input as microphone
from voice import stt
from voice import wake
import main


class TranscriptionTests(unittest.TestCase):
    def test_protocol_and_transcript(self):
        client = AsyncMock()
        client.read_event.return_value = Transcript(text="Turn on nanoleafs.").event()
        client.__aenter__.return_value = client
        with patch.object(stt.AsyncClient, "from_uri", return_value=client):
            self.assertEqual(stt.transcribe(b"\0" * 6400), "Turn on nanoleafs.")
        events = [call.args[0] for call in client.write_event.call_args_list]
        self.assertEqual([event.type for event in events],
                         ["transcribe", "audio-start", "audio-chunk", "audio-chunk", "audio-stop"])
        self.assertEqual(events[1].data["rate"], 16000)
        self.assertEqual(events[1].data["channels"], 1)
        self.assertEqual(b"".join(event.payload for event in events[2:4]), b"\0" * 6400)
        client.__aexit__.assert_awaited_once()

    def test_audio_is_peak_normalized_before_whisper(self):
        self.assertAlmostEqual(20 * math.log10(stt.TARGET_PEAK / 32768), -1, places=2)
        client = AsyncMock()
        client.read_event.return_value = Transcript(text="Open notepad.").event()
        client.__aenter__.return_value = client
        audio = struct.pack("<4h", 1000, -500, 250, 0)
        with patch.object(stt.AsyncClient, "from_uri", return_value=client):
            self.assertEqual(stt.transcribe(audio), "Open notepad.")
        chunks = [call.args[0].payload for call in client.write_event.call_args_list
                  if call.args[0].type == "audio-chunk"]
        samples = struct.unpack("<4h", b"".join(chunks))
        self.assertEqual(samples[0], stt.TARGET_PEAK)
        self.assertEqual(samples[1], -round(stt.TARGET_PEAK / 2))
        self.assertEqual(samples[3], 0)

    def test_silence_and_already_loud_audio(self):
        self.assertEqual(stt.normalize_audio(b"\0" * 8), b"\0" * 8)
        samples = struct.unpack("<2h", stt.normalize_audio(struct.pack("<2h", 30000, -15000)))
        self.assertEqual(samples, (stt.TARGET_PEAK, -round(stt.TARGET_PEAK / 2)))

    def test_disconnect(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.read_event.return_value = None
        with patch.object(stt.AsyncClient, "from_uri", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                stt.transcribe(b"\0\0")

    def test_empty_audio_skips_service(self):
        with patch.object(stt.AsyncClient, "from_uri") as connect:
            self.assertEqual(stt.transcribe(b""), "")
        connect.assert_not_called()


class RecordingTests(unittest.TestCase):
    def test_recording_polls_real_file_descriptor(self):
        # Exercise real select() so mocks cannot hide an invalid call signature.
        with tempfile.TemporaryFile() as keys:
            keys.write(b"  ")
            keys.seek(0)
            stdin = MagicMock()
            stdin.fileno.return_value = keys.fileno()
            with patch.object(microphone.sys, "stdin", stdin), \
                 patch.object(microphone.termios, "tcgetattr", return_value=[1]), \
                 patch.object(microphone.tty, "setcbreak"), \
                 patch.object(microphone.termios, "tcflush"), \
                 patch.object(microphone.termios, "tcsetattr") as restore, \
                 patch.object(microphone.sd, "RawInputStream") as stream, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(microphone.record(), b"")
            stream.return_value.__exit__.assert_called_once()
            restore.assert_called_once()

    def test_terminal_restored_after_microphone_failure(self):
        stdin = MagicMock()
        stdin.fileno.return_value = 10
        stdin.read.return_value = " "
        with patch.object(microphone.sys, "stdin", stdin), \
             patch.object(microphone.os, "read", return_value=b" "), \
             patch.object(microphone.termios, "tcgetattr", return_value=[1]), \
             patch.object(microphone.tty, "setcbreak"), \
             patch.object(microphone.termios, "tcflush"), \
             patch.object(microphone.termios, "tcsetattr") as restore, \
             patch.object(microphone.sd, "RawInputStream", side_effect=microphone.sd.PortAudioError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Microphone unavailable"):
                microphone.record()
        restore.assert_called_once_with(10, microphone.termios.TCSADRAIN, [1])

    def test_space_starts_and_stops_recording(self):
        stdin = MagicMock()
        stdin.fileno.return_value = 10

        def stream(**kwargs):
            self.assertEqual(kwargs["device"], microphone.INPUT_DEVICE)
            self.assertEqual(kwargs["channels"], 2)
            kwargs["callback"](b"\x01\x00\x02\x00\x03\x00\x04\x00",
                               2, None, None)
            return MagicMock()

        with patch.object(microphone.sys, "stdin", stdin), \
             patch.object(microphone.os, "read", side_effect=[b"x", b" ", b" "]), \
             patch.object(microphone.termios, "tcgetattr", return_value=[1]), \
             patch.object(microphone.tty, "setcbreak"), \
             patch.object(microphone.termios, "tcflush"), \
             patch.object(microphone.termios, "tcsetattr") as restore, \
             patch.object(microphone.select, "select", return_value=([stdin], [], [])), \
             patch.object(microphone.sd, "RawInputStream", side_effect=stream):
            self.assertEqual(microphone.record(), b"\x02\x00\x04\x00")
        restore.assert_called_once()


class MainTests(unittest.TestCase):
    def test_main_transcribes_then_resolves_app_name(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--manual"]), \
             patch("sys.stdin.isatty", return_value=True), \
             patch.object(microphone, "record", side_effect=[b"audio", KeyboardInterrupt()]), \
             patch.object(stt, "transcribe", return_value="Open node pad.") as transcribe, \
             patch("router.desktop.execute") as execute, redirect_stdout(output):
            main.main()
        transcribe.assert_called_once_with(b"audio")
        execute.assert_called_once_with("launch_app", "notepad")
        self.assertIn("Heard: Open node pad.", output.getvalue())
        self.assertIn("Matched: open notepad", output.getvalue())

    def test_transcript_reaches_router_after_error_and_empty_input(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--manual"]), \
             patch("sys.stdin.isatty", return_value=True), \
             patch.object(microphone, "record", side_effect=[RuntimeError("capture failed"), b"a", b"b", KeyboardInterrupt()]), \
             patch.object(stt, "transcribe", side_effect=["", "Turn on nanoleafs."]), \
             patch("router.home_assistant.call_service") as service, redirect_stdout(output):
            main.main()
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")
        self.assertIn("Heard: Turn on nanoleafs.", output.getvalue())
        self.assertIn("No speech recognized", output.getvalue())
        self.assertIn("Goodbye.", output.getvalue())

    def test_text_mode(self):
        with patch("sys.argv", ["main.py", "--text"]), \
             patch("builtins.input", side_effect=["turn on nanoleafs", "exit"]), \
             patch("router.home_assistant.call_service") as service, redirect_stdout(io.StringIO()):
            main.main()
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")

    def test_default_wake_mode_works_without_a_terminal(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py"]), \
             patch("sys.stdin.isatty", return_value=False) as isatty, \
             patch.object(wake, "record", side_effect=[b"audio", KeyboardInterrupt()]) as record, \
             patch.object(wake, "strip_wake_phrase", return_value="turn on nanoleafs") as strip, \
             patch.object(stt, "transcribe", return_value="Athena, turn on nanoleafs.") as transcribe, \
             patch("router.home_assistant.call_service") as service, redirect_stdout(output):
            main.main()
        isatty.assert_not_called()
        self.assertEqual(record.call_count, 2)
        transcribe.assert_called_once_with(b"audio")
        strip.assert_called_once_with("Athena, turn on nanoleafs.")
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")
        self.assertIn("Heard: Athena, turn on nanoleafs.", output.getvalue())

    def test_empty_automatic_capture_skips_transcription(self):
        with patch("sys.argv", ["main.py"]), \
             patch.object(wake, "record", side_effect=[b"", b"audio", KeyboardInterrupt()]), \
             patch.object(wake, "strip_wake_phrase", side_effect=lambda text: text), \
             patch.object(stt, "transcribe", return_value="turn on nanoleafs") as transcribe, \
             patch("router.home_assistant.call_service") as service, redirect_stdout(io.StringIO()):
            main.main()
        transcribe.assert_called_once_with(b"audio")
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")

    def test_wake_only_transcript_does_not_route_a_command(self):
        with patch("sys.argv", ["main.py"]), \
             patch.object(wake, "record", side_effect=[b"audio", KeyboardInterrupt()]), \
             patch.object(wake, "strip_wake_phrase", return_value=""), \
             patch.object(stt, "transcribe", return_value="Athena."), \
             patch.object(main, "route") as route, redirect_stdout(io.StringIO()):
            main.main()
        route.assert_not_called()

    def test_wake_setup_error_stops_instead_of_retrying_forever(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py"]), \
             patch.object(wake, "record", side_effect=wake.WakeSetupError("Unknown wake model.")) as record, \
             patch.object(main, "route") as route, redirect_stdout(output):
            result = main.main()
        self.assertEqual(result, 1)
        record.assert_called_once_with()
        route.assert_not_called()
        self.assertIn("Unknown wake model", output.getvalue())

    def test_manual_mode_requires_a_terminal(self):
        with patch("sys.argv", ["main.py", "--manual"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch.object(microphone, "record") as record, redirect_stdout(io.StringIO()):
            main.main()
        record.assert_not_called()

    def test_sigterm_stops_recording_and_restores_original_signal_handler(self):
        handlers = {}
        previous = object()

        def register_signal(signum, handler):
            handlers[signum] = handler
            return previous

        def stopped_recording():
            handlers[main.signal.SIGTERM](main.signal.SIGTERM, None)

        with patch("sys.argv", ["main.py"]), \
             patch.object(main.signal, "signal", side_effect=register_signal) as register, \
             patch.object(wake, "record", side_effect=stopped_recording), \
             patch.object(main, "route") as route, redirect_stdout(io.StringIO()):
            result = main.main()
        self.assertEqual(result, 0)
        route.assert_not_called()
        self.assertEqual(register.call_count, 2)
        register.assert_called_with(main.signal.SIGTERM, previous)

    def test_text_mode_does_not_import_voice_dependencies(self):
        original_import = __import__

        def import_without_voice(name, *args, **kwargs):
            if name == "voice" or name.startswith("voice."):
                raise ImportError("Audio dependencies are deliberately unavailable.")
            return original_import(name, *args, **kwargs)

        with patch("sys.argv", ["main.py", "--text"]), \
             patch("builtins.__import__", side_effect=import_without_voice), \
             patch("builtins.input", side_effect=["turn on nanoleafs", "exit"]), \
             patch("router.home_assistant.call_service") as service, redirect_stdout(io.StringIO()):
            main.main()
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")


if __name__ == "__main__":
    unittest.main()
