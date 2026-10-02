import asyncio
import struct
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ovos_dinkum_listener.plugins import FakeStreamingSTT
from ovos_dinkum_listener.voice_loop import DinkumVoiceLoop, ListeningState
from ovos_plugin_manager.utils.audio import AudioData
from ovos_vad_plugin_webrtcvad import WebRTCVAD
from wyoming.asr import Transcript
from wyoming.event import Event

from athena_ovos import plugins


class MicrophoneTests(unittest.TestCase):
    def setUp(self):
        self.mic = plugins.AthenaMicrophone(chunk_size=8)
        self.stream = MagicMock()
        self.open_stream = patch.object(plugins.sd, "RawInputStream", return_value=self.stream)
        self.factory = self.open_stream.start()
        self.addCleanup(self.open_stream.stop)
        self.addCleanup(lambda: self.mic.stop())

    def capture(self, samples):
        pcm = struct.pack("=" + "h" * len(samples), *samples)
        self.mic._capture(pcm, len(samples) // self.mic.capture_channels, None, None)

    def test_processed_second_channel_reaches_ovos_without_mixing(self):
        self.mic.start()
        self.capture([3000, 11, -3000, -12, 5000, 13, -5000, -14])
        self.assertEqual(self.mic.read_chunk(), struct.pack("<4h", 11, -12, 13, -14))
        options = self.factory.call_args.kwargs
        self.assertEqual((options["samplerate"], options["channels"], options["dtype"]),
                         (16000, 2, "int16"))
        self.assertEqual(options["device"], "reSpeaker Flex XVF3800")
        self.assertEqual(options["blocksize"], 4)
        self.assertEqual(self.mic.sample_channels, 1)

    def test_default_microphone_chunk_matches_real_webrtc_vad_contract(self):
        microphone = plugins.AthenaMicrophone()
        vad = WebRTCVAD({"frame_duration_ms": 20, "vad_mode": 2}, sample_rate=16000)
        self.assertEqual(microphone.frames_per_chunk, 320)
        self.assertEqual(microphone.seconds_per_chunk, 0.02)
        self.assertTrue(vad.is_silence(bytes(microphone.chunk_size)))

    def test_configured_channel_is_selected(self):
        self.mic = plugins.AthenaMicrophone(chunk_size=8, source_channel=1)
        self.mic.start()
        self.capture([11, 3000, 12, 5000])
        self.assertEqual(self.mic.read_chunk(), struct.pack("<2h", 11, 12))

    def test_capture_status_fails_instead_of_transcribing_corrupt_audio(self):
        self.mic.start()
        self.mic._capture(b"", 0, None, "input overflow")
        with self.assertRaisesRegex(RuntimeError, "input overflow"):
            self.mic.read_chunk()

    def test_incomplete_capture_frame_is_reported(self):
        self.mic.start()
        self.mic._capture(b"\0\0", 1, None, None)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.mic.read_chunk()

    def test_stalled_capture_is_reported(self):
        self.mic.timeout = 0.001
        self.mic.start()
        with self.assertRaisesRegex(RuntimeError, "stopped producing audio"):
            self.mic.read_chunk()

    def test_queue_discards_old_audio_while_ovos_is_busy(self):
        self.mic = plugins.AthenaMicrophone(chunk_size=2, queue_maxsize=2)
        self.mic.start()
        for value in [1, 2, 3, 4]:
            self.capture([0, value])
        self.assertEqual(self.mic.read_chunk(), struct.pack("<h", 3))
        self.assertEqual(self.mic.read_chunk(), struct.pack("<h", 4))

    def test_start_is_idempotent_and_stop_closes_once(self):
        self.mic.start()
        self.mic.start()
        self.factory.assert_called_once()
        self.mic.stop()
        self.mic.stop()
        self.stream.stop.assert_called_once()
        self.stream.close.assert_called_once()
        self.assertIsNone(self.mic.read_chunk())

    def test_restart_does_not_replay_audio_from_previous_session(self):
        self.mic.start()
        self.capture([1, 2])
        self.mic.stop()
        self.mic.start()
        self.capture([8, 9])
        self.assertEqual(self.mic.read_chunk(), struct.pack("<h", 9))

    def test_stop_unblocks_an_ovos_reader(self):
        self.mic.start()
        waiting = threading.Event()
        received = []

        def read():
            waiting.set()
            received.append(self.mic.read_chunk())

        reader = threading.Thread(target=read)
        reader.start()
        self.assertTrue(waiting.wait(1))
        self.mic.stop()
        reader.join(1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(received, [None])

    def test_start_failure_closes_the_open_stream(self):
        self.stream.start.side_effect = RuntimeError("device unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.mic.start()
        self.stream.close.assert_called_once()
        self.assertIsNone(self.mic.read_chunk())

    def test_close_runs_even_when_stop_fails(self):
        self.mic.start()
        self.stream.stop.side_effect = RuntimeError("stop failed")
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            self.mic.stop()
        self.stream.close.assert_called_once()

    def test_invalid_capture_configuration_is_rejected(self):
        for config in [{"source_channel": 3}, {"sample_channels": 2},
                       {"sample_rate": 48000}, {"chunk_size": 3},
                       {"queue_maxsize": 0}, {"timeout": 0}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                plugins.AthenaMicrophone(**config)


class WhisperSTTTests(unittest.TestCase):
    def setUp(self):
        self.engine = plugins.AthenaWhisperSTT({"lang": "en-US"})
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.read_event.return_value = Transcript(text=" Open Notepad. ").event()
        self.connect = patch.object(plugins.AsyncClient, "from_uri", return_value=self.client)
        self.factory = self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_ovos_audio_uses_wyoming_protocol_and_local_whisper(self):
        audio = AudioData(b"\0" * 6400, 16000, 2)
        self.assertEqual(self.engine.execute(audio), "Open Notepad.")
        self.factory.assert_called_once_with("tcp://127.0.0.1:10300", connect_timeout=5)
        events = [call.args[0] for call in self.client.write_event.call_args_list]
        self.assertEqual([event.type for event in events],
                         ["transcribe", "audio-start", "audio-chunk", "audio-chunk", "audio-stop"])
        self.assertEqual(events[0].data["language"], "en")
        self.assertEqual(events[1].data["rate"], 16000)
        self.assertEqual(events[1].data["width"], 2)
        self.assertEqual(events[1].data["channels"], 1)
        self.assertEqual(b"".join(event.payload for event in events[2:4]), b"\0" * 6400)
        self.client.__aexit__.assert_awaited_once()

    def test_recording_is_converted_before_peak_normalization(self):
        audio = MagicMock()
        audio.get_raw_data.return_value = struct.pack("<4h", 1000, -500, 250, 0)
        self.engine.execute(audio, "en-GB")
        audio.get_raw_data.assert_called_once_with(convert_rate=16000, convert_width=2)
        events = [call.args[0] for call in self.client.write_event.call_args_list]
        payload = b"".join(event.payload for event in events if event.type == "audio-chunk")
        self.assertEqual(struct.unpack("<4h", payload),
                         (plugins.TARGET_PEAK, -round(plugins.TARGET_PEAK / 2),
                          round(plugins.TARGET_PEAK / 4), 0))

    def test_listener_stream_wrapper_delivers_recording_to_whisper(self):
        wrapper = FakeStreamingSTT(self.engine, {"lang": "en-US"})
        wrapper.stream_start("en-US")
        try:
            wrapper.stream_data(b"\0" * 640)
            # Finish the real listener's buffering thread before requesting STT.
            wrapper.queue.put(None)
            wrapper.stream.join(1)
            self.assertFalse(wrapper.stream.is_alive())
            self.assertEqual(wrapper.transcribe(lang="en-US"), [("Open Notepad.", 1.0)])
        finally:
            wrapper.stream_stop()
        events = [call.args[0] for call in self.client.write_event.call_args_list]
        self.assertEqual(b"".join(event.payload for event in events if event.type == "audio-chunk"),
                         b"\0" * 640)

    def test_empty_recording_does_not_contact_whisper(self):
        self.assertEqual(self.engine.execute(AudioData(b"", 16000, 2)), "")
        self.factory.assert_not_called()

    def test_remote_disconnect_is_reported_and_connection_closes(self):
        self.client.read_event.return_value = None
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.engine.execute(AudioData(b"\0\0", 16000, 2))
        self.client.__aexit__.assert_awaited_once()

    def test_server_error_is_reported(self):
        self.client.read_event.return_value = Event(type="error")
        with self.assertRaisesRegex(RuntimeError, "transcription error"):
            self.engine.execute(AudioData(b"\0\0", 16000, 2))

    def test_connection_failure_is_reported(self):
        self.client.__aenter__.side_effect = ConnectionRefusedError()
        with self.assertRaisesRegex(RuntimeError, "Cannot reach Whisper"):
            self.engine.execute(AudioData(b"\0\0", 16000, 2))

    def test_timeout_cancels_pending_read_and_closes_connection(self):
        cancelled = []

        async def pending_read():
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        self.engine.timeout = 0.01
        self.client.read_event.side_effect = pending_read
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.engine.execute(AudioData(b"\0\0", 16000, 2))
        self.assertEqual(cancelled, [True])
        self.client.__aexit__.assert_awaited_once()

    def test_plugin_config_overrides_uri_and_environment_default(self):
        with patch.dict("os.environ", {"WHISPER_URI": "tcp://whisper:10300"}):
            engine = plugins.AthenaWhisperSTT({"lang": "en-US"})
            self.assertEqual(engine.uri, "tcp://whisper:10300")
            configured = plugins.AthenaWhisperSTT({"uri": "tcp://other:10301", "lang": "en-US"})
            self.assertEqual(configured.uri, "tcp://other:10301")

    def test_internet_is_not_required_by_local_stt(self):
        self.assertFalse(self.engine.runtime_requirements.requires_internet)
        self.assertFalse(self.engine.runtime_requirements.internet_before_load)

    def test_invalid_audio_and_unsupported_language_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "complete 16-bit"):
            plugins.normalize_audio(b"\0")
        with self.assertRaisesRegex(ValueError, "English"):
            self.engine.execute(AudioData(b"\0\0", 16000, 2), "fr-FR")
        self.factory.assert_not_called()


class ListenerRecordingTests(unittest.TestCase):
    def setUp(self):
        self.microphone = plugins.AthenaMicrophone()
        self.vad = MagicMock()
        self.loop = DinkumVoiceLoop(
            mic=self.microphone, hotwords=MagicMock(), stt=MagicMock(),
            fallback_stt=None, vad=self.vad, transformers=MagicMock(),
            speech_seconds=0.1, silence_seconds=0.8,
            timeout_seconds=30, timeout_seconds_with_silence=5,
            num_stt_rewind_chunks=10, vad_pre_wake_enabled=False,
        )
        self.loop.reset_speech_timer()
        self.loop.state = ListeningState.BEFORE_COMMAND
        self.chunk = bytes(self.microphone.chunk_size)

    def start_command(self):
        self.vad.is_silence.return_value = False
        for _ in range(5):
            self.loop._before_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.IN_COMMAND)

    def test_real_listener_records_speech_then_stops_after_silence(self):
        self.start_command()
        self.vad.is_silence.return_value = True
        for _ in range(39):
            self.loop._in_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.IN_COMMAND)
        self.loop._in_cmd(self.chunk)
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        self.assertEqual(self.loop.stt_audio_bytes, self.chunk * 45)

    def test_real_listener_bounds_wait_when_wake_word_has_no_command(self):
        self.vad.is_silence.return_value = True
        chunks = 0
        while self.loop.state == ListeningState.BEFORE_COMMAND and chunks < 252:
            self.loop._before_cmd(self.chunk)
            chunks += 1
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        self.assertAlmostEqual(chunks * self.microphone.seconds_per_chunk, 5, delta=0.021)

    def test_real_listener_caps_continuous_speech(self):
        self.start_command()
        chunks = 5
        while self.loop.state == ListeningState.IN_COMMAND and chunks < 1502:
            self.loop._in_cmd(self.chunk)
            chunks += 1
        self.assertEqual(self.loop.state, ListeningState.AFTER_COMMAND)
        self.assertAlmostEqual(chunks * self.microphone.seconds_per_chunk, 30, delta=0.021)


if __name__ == "__main__":
    unittest.main()
