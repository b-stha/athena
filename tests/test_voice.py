import unittest
import io
import tempfile
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, MagicMock, patch

from wyoming.asr import Transcript

from voice import input as microphone
from voice import stt
import main


class TranscriptionTests(unittest.TestCase):
    def test_protocol_and_transcript(self):
        client = AsyncMock()
        client.read_event.return_value = Transcript(text="Turn on desk lights.").event()
        client.__aenter__.return_value = client
        with patch.object(stt.AsyncClient, "from_uri", return_value=client):
            self.assertEqual(stt.transcribe(b"\0" * 6400), "Turn on desk lights.")
        events = [call.args[0] for call in client.write_event.call_args_list]
        self.assertEqual([event.type for event in events],
                         ["transcribe", "audio-start", "audio-chunk", "audio-chunk", "audio-stop"])
        self.assertEqual(events[1].data["rate"], 16000)
        self.assertEqual(b"".join(event.payload for event in events[2:4]), b"\0" * 6400)
        client.__aexit__.assert_awaited_once()

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
            kwargs["callback"](b"\0\0", 1, None, None)
            return MagicMock()

        with patch.object(microphone.sys, "stdin", stdin), \
             patch.object(microphone.os, "read", side_effect=[b"x", b" ", b" "]), \
             patch.object(microphone.termios, "tcgetattr", return_value=[1]), \
             patch.object(microphone.tty, "setcbreak"), \
             patch.object(microphone.termios, "tcflush"), \
             patch.object(microphone.termios, "tcsetattr") as restore, \
             patch.object(microphone.select, "select", return_value=([stdin], [], [])), \
             patch.object(microphone.sd, "RawInputStream", side_effect=stream):
            self.assertEqual(microphone.record(), b"\0\0")
        restore.assert_called_once()


class MainTests(unittest.TestCase):
    def test_transcript_reaches_router_after_error_and_empty_input(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py"]), \
             patch("sys.stdin.isatty", return_value=True), \
             patch.object(microphone, "record", side_effect=[RuntimeError("capture failed"), b"a", b"b", KeyboardInterrupt()]), \
             patch.object(stt, "transcribe", side_effect=["", "Turn on desk lights."]), \
             patch("router.home_assistant.call_service") as service, redirect_stdout(output):
            main.main()
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")
        self.assertIn("Heard: Turn on desk lights.", output.getvalue())
        self.assertIn("No speech recognized", output.getvalue())
        self.assertIn("Goodbye.", output.getvalue())

    def test_text_mode(self):
        with patch("sys.argv", ["main.py", "--text"]), \
             patch("builtins.input", side_effect=["turn on desk lights", "exit"]), \
             patch("router.home_assistant.call_service") as service, redirect_stdout(io.StringIO()):
            main.main()
        service.assert_called_once_with("light", "turn_on", "light.nanoleafs")


if __name__ == "__main__":
    unittest.main()
