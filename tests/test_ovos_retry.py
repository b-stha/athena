"""Exercise listener retry after a real Wyoming socket times out; no microphone."""

import socketserver
import threading
import time
import unittest
from unittest.mock import Mock

from ovos_dinkum_listener.service import OVOSDinkumVoiceService
from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop, ListeningState
from wyoming.asr import Transcript
from wyoming.event import read_event, write_event

from athena_ovos.plugins import AthenaMicrophone, AthenaWhisperSTT
from athena_ovos.streaming import BufferedWhisperSTT


class ListenerRetryTests(unittest.TestCase):
    def setUp(self):
        class WyomingHandler(socketserver.StreamRequestHandler):
            def handle(handler):
                handler.connection.settimeout(3)
                while event := read_event(handler.rfile):
                    if event.type != "audio-stop":
                        continue
                    with handler.server.request_lock:
                        handler.server.requests += 1
                        first = handler.server.requests == 1
                    if first:
                        # No transcript: wait for the client's deadline to close
                        # the socket. This stub does not simulate cancellation
                        # of inference in the real Whisper server.
                        if read_event(handler.rfile) is None:
                            handler.server.disconnected.set()
                    else:
                        write_event(Transcript(text="Open Notepad.").event(), handler.wfile)
                        handler.wfile.flush()
                    return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), WyomingHandler)
        self.server.daemon_threads = True
        self.server.request_lock = threading.Lock()
        self.server.requests = 0
        self.server.disconnected = threading.Event()
        server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        server_thread.start()

        def close_server():
            self.server.shutdown()
            self.server.server_close()
            server_thread.join(2)
        self.addCleanup(close_server)

        engine = AthenaWhisperSTT({"lang": "en-US", "timeout": 0.2,
                                  "uri": f"tcp://127.0.0.1:{self.server.server_address[1]}"})
        self.stt = BufferedWhisperSTT(engine, {"lang": "en-US"})
        self.addCleanup(self.stt.shutdown)
        self.chunk = b"\x10\x00" * 320
        self.hotwords = Mock()
        self.hotwords.found.return_value = "athena"
        self.hotwords.get_ww.return_value = {}
        self.vad = Mock()
        transformers = Mock()
        transformers.transform.return_value = (self.chunk, {"lang": "en-us"})
        self.loop = DinkumVoiceLoop(
            mic=AthenaMicrophone(), hotwords=self.hotwords, stt=self.stt,
            fallback_stt=None, vad=self.vad, transformers=transformers,
            speech_seconds=0.1, silence_seconds=0.8, remove_silence=False,
            timeout_seconds=30, timeout_seconds_with_silence=5,
            vad_pre_wake_enabled=False,
        )
        # Use the real service's empty/successful transcription event mapping,
        # while avoiding service initialization and physical microphone access.
        service = object.__new__(OVOSDinkumVoiceService)
        service.config = {}
        service.bus = Mock()
        service.voice_loop = self.loop
        self.bus = service.bus
        self.loop.text_callback = service._stt_text

    def record_command(self):
        self.assertTrue(self.loop._detect_ww(self.chunk))
        self.assertEqual(self.loop.state, ListeningState.BEFORE_COMMAND)
        self.vad.is_silence.return_value = False
        for _ in range(6):
            if self.loop.state != ListeningState.BEFORE_COMMAND:
                break
            self.loop._before_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.IN_COMMAND)
        self.vad.is_silence.return_value = True
        for _ in range(41):
            if self.loop.state != ListeningState.IN_COMMAND:
                break
            self.loop._in_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        # Wait for the worker to buffer every frame before final transcription.
        expected = len(self.loop.stt_audio_bytes) + len(self.chunk)
        deadline = time.monotonic() + 1
        while len(self.stt.stream.buffer) < expected:
            if time.monotonic() >= deadline:
                self.fail("OVOS did not finish buffering the synthetic recording.")
            time.sleep(0.005)

    def test_timeout_releases_socket_and_listener_accepts_next_command(self):
        self.record_command()
        first_worker = self.stt.stream
        started = time.monotonic()
        self.loop._after_cmd(self.chunk)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.server.disconnected.wait(1), "Timed-out connection did not close.")
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)
        self.assertEqual(self.loop.stt_audio_bytes, b"")
        self.assertFalse(self.loop.stt_chunks)
        self.hotwords.reset.assert_called_once()
        events = [call.args[0] for call in self.bus.emit.call_args_list]
        self.assertEqual([event.msg_type for event in events],
                         ["recognizer_loop:speech.recognition.unknown"])

        self.record_command()
        self.assertFalse(first_worker.is_alive())
        self.loop._after_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)
        events = [call.args[0] for call in self.bus.emit.call_args_list]
        self.assertEqual([event.msg_type for event in events],
                         ["recognizer_loop:speech.recognition.unknown", "recognizer_loop:utterance"])
        self.assertEqual(events[1].data["utterances"], ["Open Notepad."])
        self.assertEqual(self.server.requests, 2)
        self.assertEqual(self.hotwords.reset.call_count, 2)


if __name__ == "__main__":
    unittest.main()
