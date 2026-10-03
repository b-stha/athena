"""Exercise OVOS speech confirmation and discard using only synthetic PCM."""

from dataclasses import fields
import socketserver
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ovos_bus_client.message import Message
from ovos_dinkum_listener.service import OVOSDinkumVoiceService
from ovos_dinkum_listener.voice_loop import ListeningMode, ListeningState
from wyoming.asr import Transcript
from wyoming.event import read_event, write_event

from athena_ovos.listener import AthenaVoiceLoop, AthenaVoiceService
from athena_ovos.plugins import AthenaMicrophone, AthenaWhisperSTT
from athena_ovos.streaming import BufferedWhisperSTT


class SpeechDiscardTests(unittest.TestCase):
    def setUp(self):
        class WyomingHandler(socketserver.StreamRequestHandler):
            def handle(handler):
                handler.connection.settimeout(3)
                with handler.server.request_lock:
                    handler.server.connections += 1
                audio = bytearray()
                while event := read_event(handler.rfile):
                    if event.type == "audio-chunk":
                        audio.extend(event.payload or b"")
                    elif event.type == "audio-stop":
                        with handler.server.request_lock:
                            handler.server.requests.append(bytes(audio))
                            first = len(handler.server.requests) == 1
                        if first and handler.server.stall_first:
                            if read_event(handler.rfile) is None:
                                handler.server.disconnected.set()
                        else:
                            write_event(Transcript(text="Open Notepad.").event(), handler.wfile)
                        return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), WyomingHandler)
        self.server.daemon_threads = True
        self.server.request_lock = threading.Lock()
        self.server.requests = []
        self.server.connections = 0
        self.server.stall_first = False
        self.server.disconnected = threading.Event()
        server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        server_thread.start()

        def close_server():
            self.server.shutdown()
            self.server.server_close()
            server_thread.join(2)
        self.addCleanup(close_server)

        self.engine = AthenaWhisperSTT({
            "lang": "en-US", "timeout": 2,
            "uri": f"tcp://127.0.0.1:{self.server.server_address[1]}",
        })
        self.stt = BufferedWhisperSTT(self.engine, {"lang": "en-US"})
        self.addCleanup(self.stt.shutdown)
        # Quiet synthetic PCM verifies that the guard introduces no amplitude
        # cutoff. Stub VAD classifications do not establish real-room accuracy.
        self.chunk = b"\x01\x00" * 320
        self.mic = AthenaMicrophone()
        self.hotwords = Mock()
        self.hotwords.found.return_value = "athena"
        self.hotwords.get_ww.return_value = {}
        self.vad = Mock()
        self.vad.extract_speech.return_value = None
        self.transformers = Mock()
        self.transformers.transform.return_value = (self.chunk, {"lang": "en-us"})
        self.record_end = Mock()
        self.loop = AthenaVoiceLoop(
            mic=self.mic, hotwords=self.hotwords, stt=self.stt,
            fallback_stt=None, vad=self.vad, transformers=self.transformers,
            speech_seconds=0.1, silence_seconds=0.8, remove_silence=True,
            timeout_seconds=30, timeout_seconds_with_silence=5,
            record_end_callback=self.record_end, vad_pre_wake_enabled=False,
        )
        # Exercise native event mapping and shutdown without constructing a
        # service, opening a device or connecting to a messagebus.
        self.service = object.__new__(AthenaVoiceService)
        self.service.config = {}
        self.service.bus = Mock()
        self.service.voice_loop = self.loop
        self.service.mic = self.mic
        self.service.hotwords = self.hotwords
        self.service.vad = self.vad
        self.service.stt = self.stt
        self.service.fallback_stt = None
        self.service.transformers = self.transformers
        self.service._load_lock = threading.RLock()
        self.service._shutdown_event = threading.Event()
        self.service.status = Mock()
        self.service.disable_hotword_reload = True
        self.service.validate_source = False
        self.loop.text_callback = self.service._stt_text

    def bus_events(self):
        return [call.args[0] for call in self.service.bus.emit.call_args_list]

    def wake(self):
        self.assertTrue(self.loop._detect_ww(self.chunk))
        self.assertEqual(self.loop.state, ListeningState.BEFORE_COMMAND)
        self.assertFalse(self.loop._speech_confirmed)
        return self.stt.stream

    def finish_no_speech(self):
        self.vad.is_silence.return_value = True
        for _ in range(252):
            if self.loop.state != ListeningState.BEFORE_COMMAND:
                break
            self.loop._before_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        self.assertFalse(self.loop._speech_confirmed)
        self.loop._after_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)

    def record_speech(self):
        worker = self.wake()
        self.vad.is_silence.return_value = False
        for _ in range(6):
            if self.loop.state != ListeningState.BEFORE_COMMAND:
                break
            self.loop._before_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.IN_COMMAND)
        self.assertTrue(self.loop._speech_confirmed)
        self.vad.is_silence.return_value = True
        for _ in range(41):
            if self.loop.state != ListeningState.IN_COMMAND:
                break
            self.loop._in_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        # The native wake detector rewinds one synthetic wake chunk in addition
        # to the recording held in stt_audio_bytes.
        expected = len(self.loop.stt_audio_bytes) + len(self.chunk)
        deadline = time.monotonic() + 1
        while len(worker.buffer) < expected:
            if not worker.is_alive() or time.monotonic() >= deadline:
                self.fail("OVOS did not finish buffering the synthetic recording.")
            time.sleep(0.005)
        return worker

    def assert_discarded(self, worker):
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker.buffer), 0)
        self.assertIsNone(self.stt.stream)
        self.assertIsNone(self.stt.queue)
        self.assertTrue(self.stt.transcript_ready.is_set())
        self.assertEqual(self.loop.stt_audio_bytes, b"")
        self.assertFalse(self.loop.stt_chunks)
        self.assertFalse(self.loop._speech_confirmed)

    def test_no_speech_skips_transcription_and_next_confirmed_command_succeeds(self):
        worker = self.wake()
        self.finish_no_speech()
        self.assert_discarded(worker)
        self.vad.extract_speech.assert_not_called()
        self.assertEqual(self.server.connections, 0)
        self.assertEqual(self.server.requests, [])
        self.record_end.assert_called_once()
        self.hotwords.reset.assert_called_once()
        self.assertEqual([event.msg_type for event in self.bus_events()],
                         ["recognizer_loop:speech.recognition.unknown"])

        self.record_speech()
        self.loop._after_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)
        self.assertFalse(self.loop._speech_confirmed)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.connections, 1)
        events = self.bus_events()
        self.assertEqual([event.msg_type for event in events],
                         ["recognizer_loop:speech.recognition.unknown", "recognizer_loop:utterance"])
        self.assertEqual(events[1].data["utterances"], ["Open Notepad."])
        self.assertEqual(self.record_end.call_count, 2)
        self.assertEqual(self.hotwords.reset.call_count, 2)

    def test_successful_speech_does_not_enable_transcription_of_next_silent_session(self):
        first = self.record_speech()
        self.loop._after_cmd(self.chunk)
        self.assertEqual(len(self.server.requests), 1)
        worker = self.wake()
        self.assertFalse(first.is_alive())
        self.finish_no_speech()
        self.assert_discarded(worker)
        self.vad.extract_speech.assert_not_called()
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.connections, 1)
        self.assertEqual([event.msg_type for event in self.bus_events()],
                         ["recognizer_loop:utterance", "recognizer_loop:speech.recognition.unknown"])

    def test_brief_noise_pulses_without_required_contiguous_speech_are_discarded(self):
        worker = self.wake()
        self.vad.is_silence.return_value = True
        self.loop._before_cmd(self.chunk)
        for _ in range(3):
            self.vad.is_silence.return_value = False
            for _ in range(3):
                self.loop._before_cmd(self.chunk)
            self.assertEqual(self.loop.state, ListeningState.BEFORE_COMMAND)
            self.assertFalse(self.loop._speech_confirmed)
            self.vad.is_silence.return_value = True
            self.loop._before_cmd(self.chunk)
        self.finish_no_speech()
        self.assert_discarded(worker)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.server.connections, 0)
        self.vad.extract_speech.assert_not_called()

    def test_native_explicit_listen_resets_confirmation_before_discard(self):
        self.loop._is_running = True
        self.loop._speech_confirmed = True
        self.service._handle_listen(Message("mycroft.mic.listen"))
        self.assertEqual(self.loop.state, ListeningState.BEFORE_COMMAND)
        self.assertFalse(self.loop._speech_confirmed)
        worker = self.stt.stream
        self.finish_no_speech()
        self.assert_discarded(worker)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.server.connections, 0)

    def test_reset_start_and_native_stop_never_finalize_partial_audio(self):
        self.loop._speech_confirmed = True
        self.loop.reset_speech_timer()
        self.assertFalse(self.loop._speech_confirmed)
        self.loop._speech_confirmed = True
        self.loop.reset_state()
        self.assertFalse(self.loop._speech_confirmed)
        with patch("ovos_dinkum_listener.voice_loop.voice_loop.Configuration",
                   return_value={"listener": {}}):
            self.loop._speech_confirmed = True
            self.loop.start()
        self.assertFalse(self.loop._speech_confirmed)
        worker = self.wake()
        self.vad.is_silence.return_value = False
        self.loop._before_cmd(self.chunk)
        self.service.stop()
        self.assertFalse(self.loop.running)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.stt.stream)
        self.assertEqual(len(worker.buffer), 0)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.server.connections, 0)
        self.assertTrue(self.service._shutdown_event.is_set())
        self.assertEqual(self.bus_events(), [])

    def test_stalled_transcription_then_discard_then_success_use_exactly_two_requests(self):
        self.server.stall_first = True
        self.engine.timeout = 0.2
        self.record_speech()
        started = time.monotonic()
        self.loop._after_cmd(self.chunk)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.server.disconnected.wait(1))
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)
        self.assertFalse(self.loop._speech_confirmed)
        self.assertEqual(len(self.server.requests), 1)

        discarded = self.wake()
        self.finish_no_speech()
        self.assert_discarded(discarded)
        self.assertEqual(len(self.server.requests), 1)
        self.record_speech()
        self.loop._after_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.DETECT_WAKEWORD)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.connections, 2)
        events = self.bus_events()
        self.assertEqual([event.msg_type for event in events],
                         ["recognizer_loop:speech.recognition.unknown",
                          "recognizer_loop:speech.recognition.unknown", "recognizer_loop:utterance"])
        self.assertEqual(events[-1].data["utterances"], ["Open Notepad."])

    def test_native_continuous_wait_confirms_speech_before_transcription(self):
        self.loop.listen_mode = ListeningMode.CONTINUOUS
        self.loop.reset_speech_timer()
        # run() initializes this timer before the native continuous FSM starts.
        self.loop.silence_seconds_left = self.loop.silence_seconds
        self.loop.state = ListeningState.WAITING_CMD
        self.vad.is_silence.return_value = False
        for _ in range(6):
            if self.loop.state != ListeningState.WAITING_CMD:
                break
            self.loop._wait_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.IN_COMMAND)
        self.assertTrue(self.loop._speech_confirmed)
        self.vad.is_silence.return_value = True
        for _ in range(41):
            if self.loop.state != ListeningState.IN_COMMAND:
                break
            self.loop._in_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        worker = self.stt.stream
        expected = len(self.loop.stt_audio_bytes)
        deadline = time.monotonic() + 1
        while len(worker.buffer) < expected:
            if not worker.is_alive() or time.monotonic() >= deadline:
                self.fail("Continuous recording was not buffered.")
            time.sleep(0.005)
        self.loop._after_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.WAITING_CMD)
        self.assertFalse(self.loop._speech_confirmed)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual([event.msg_type for event in self.bus_events()],
                         ["recognizer_loop:utterance"])

    def test_service_factory_preserves_native_settings_plugins_and_callbacks(self):
        settings = {
            "instant_listen": False, "speech_begin": 0.16, "silence_end": 0.6,
            "recording_timeout": 21, "recording_timeout_with_silence": 3,
            "recording_mode_max_silence_seconds": 8, "utterance_chunks_to_rewind": 4,
            "wakeword_chunks_to_save": 12, "remove_silence": True,
            "min_stt_confidence": 0.7, "max_transcripts": 2,
        }
        native = OVOSDinkumVoiceService._init_voice_loop(self.service, settings)
        guarded = self.service._init_voice_loop(settings)
        self.assertIsInstance(guarded, AthenaVoiceLoop)
        for field in fields(native):
            if field.init:
                with self.subTest(setting=field.name):
                    self.assertEqual(getattr(guarded, field.name), getattr(native, field.name))
        self.assertIs(guarded.stt, self.stt)
        self.assertIs(guarded.mic, self.mic)
        self.assertEqual(guarded.text_callback, self.service._stt_text)
        self.assertEqual(guarded.record_end_callback, self.service._record_end_signal)
        self.assertFalse(guarded._speech_confirmed)


if __name__ == "__main__":
    unittest.main()
