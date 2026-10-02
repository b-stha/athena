"""Deterministic wake/endpointing checks; never open a microphone or call services."""

import asyncio
import io
import math
import os
import struct
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from wyoming.event import Event
from wyoming.wake import Detection

from voice import audio, wake


def pcm(value=0):
    return struct.pack("<" + "h" * audio.FRAME_SAMPLES,
                       *([value] * audio.FRAME_SAMPLES))


def frames_for(seconds):
    return math.ceil(seconds * 1000 / audio.FRAME_MS)


def fake_vad():
    detector = MagicMock()
    detector.is_speech.side_effect = lambda frame, rate: frame != pcm()
    return detector


class WakePhraseTests(unittest.TestCase):
    def test_only_leading_complete_phrase_is_removed(self):
        cases = [
            ("Athena, turn on nanoleafs.", "turn on nanoleafs."),
            ("ATHENA! Open notepad.", "Open notepad."),
            ("  Athena turn off table glow", "turn off table glow"),
            ("Athena.", ""),
            ("athena", ""),
            ("", ""),
            ("open athena notes", "open athena notes"),
            ("Athenarium turn on nanoleafs", "Athenarium turn on nanoleafs"),
            ("Hey Athena turn on nanoleafs", "Hey Athena turn on nanoleafs"),
            ("turn on nanoleafs", "turn on nanoleafs"),
        ]
        with patch.dict(os.environ, {"WAKE_PHRASE": "Athena"}):
            for transcript, expected in cases:
                with self.subTest(transcript=transcript):
                    self.assertEqual(wake.strip_wake_phrase(transcript), expected)

    def test_configured_phrase_is_used_instead_of_default(self):
        with patch.dict(os.environ, {"WAKE_PHRASE": "Hey Jarvis"}):
            self.assertEqual(wake.strip_wake_phrase("Hey Jarvis, open notepad"), "open notepad")
            self.assertEqual(wake.strip_wake_phrase("Athena, open notepad"),
                             "Athena, open notepad")

    def test_environment_defaults_select_exact_single_word_athena(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = wake.WakeSettings.from_environment()
        self.assertEqual(settings.word, "athena")
        self.assertEqual(settings.phrase.casefold(), "athena")

    def test_invalid_service_or_empty_word_fails_before_capture(self):
        cases = [
            {"WAKE_URI": "http://localhost:10400"},
            {"WAKE_URI": "tcp://localhost"},
            {"WAKE_URI": "tcp://localhost:99999"},
            {"WAKE_WORD": " "},
            {"WAKE_PHRASE": " "},
        ]
        for config in cases:
            with self.subTest(config=config), patch.dict(os.environ, config, clear=True):
                with self.assertRaises(wake.WakeSetupError):
                    wake.WakeSettings.from_environment()


class SpeechRecorderTests(unittest.TestCase):
    def test_silence_expires_without_returning_audio(self):
        recorder = wake.SpeechRecorder(fake_vad())
        count = frames_for(wake.START_TIMEOUT_SECONDS)
        for index in range(count):
            self.assertEqual(recorder.process(pcm()), index == count - 1)
        self.assertEqual(recorder.audio, b"")

    def test_brief_noise_is_rejected_below_minimum_speech(self):
        recorder = wake.SpeechRecorder(fake_vad())
        short_speech = frames_for(wake.MIN_SPEECH_SECONDS) - 1
        for _ in range(short_speech):
            self.assertFalse(recorder.process(pcm(1000)))
        for _ in range(frames_for(wake.START_TIMEOUT_SECONDS) - short_speech):
            done = recorder.process(pcm())
        self.assertTrue(done)
        self.assertEqual(recorder.audio, b"")

    def test_trailing_silence_finishes_but_short_pause_does_not(self):
        recorder = wake.SpeechRecorder(fake_vad())
        first = [pcm(1000 + i) for i in range(frames_for(wake.MIN_SPEECH_SECONDS))]
        pause = [pcm()] * (frames_for(wake.END_SILENCE_SECONDS) // 2)
        second = [pcm(2000 + i) for i in range(frames_for(wake.MIN_SPEECH_SECONDS))]
        for frame in first + pause + second:
            self.assertFalse(recorder.process(frame))
        silence = frames_for(wake.END_SILENCE_SECONDS)
        for index in range(silence):
            self.assertEqual(recorder.process(pcm()), index == silence - 1)
        self.assertIn(b"".join(first + pause + second), recorder.audio)

    def test_trigger_preroll_preserves_immediate_command_audio(self):
        # Distinct samples make omission/reordering visible, including the trigger frame.
        preroll = [pcm(7001), pcm(7002), pcm(7003)]
        speech = [pcm(8000 + i) for i in range(frames_for(wake.MIN_SPEECH_SECONDS))]
        recorder = wake.SpeechRecorder(fake_vad(), preroll=preroll)
        for frame in speech:
            recorder.process(frame)
        for _ in range(frames_for(wake.END_SILENCE_SECONDS)):
            done = recorder.process(pcm())
        self.assertTrue(done)
        self.assertTrue(recorder.audio.startswith(b"".join(preroll + speech)))

    def test_sustained_speech_stops_at_maximum_recording_length(self):
        recorder = wake.SpeechRecorder(fake_vad())
        maximum = frames_for(audio.MAX_SECONDS)
        for index in range(maximum):
            self.assertEqual(recorder.process(pcm(1000)), index == maximum - 1)
        self.assertGreater(len(recorder.audio), 0)
        self.assertLessEqual(len(recorder.audio), maximum * audio.FRAME_BYTES)

    def test_wake_preroll_alone_does_not_count_as_command_speech(self):
        preroll = [pcm(7000)] * frames_for(wake.MIN_SPEECH_SECONDS * 2)
        vad = fake_vad()
        recorder = wake.SpeechRecorder(vad, preroll=preroll)
        vad.is_speech.assert_not_called()
        for _ in range(frames_for(wake.START_TIMEOUT_SECONDS)):
            done = recorder.process(pcm())
        self.assertTrue(done)
        self.assertEqual(recorder.audio, b"")

    def test_wake_tail_does_not_end_capture_before_a_paused_command(self):
        recorder = wake.SpeechRecorder(fake_vad(), wake_guard_ms=wake.WAKE_TAIL_MS)
        wake_tail = [pcm(7000)] * frames_for(wake.MIN_SPEECH_SECONDS)
        pause = [pcm()] * frames_for(1)
        command = [pcm(1000)] * frames_for(wake.MIN_SPEECH_SECONDS * 2)
        for frame in wake_tail + pause + command:
            self.assertFalse(recorder.process(frame))
        for _ in range(frames_for(wake.END_SILENCE_SECONDS)):
            done = recorder.process(pcm())
        self.assertTrue(done)
        self.assertTrue(recorder.audio.startswith(b"".join(wake_tail + pause + command)))

    def test_short_immediate_speech_inside_guard_is_preserved_at_timeout(self):
        recorder = wake.SpeechRecorder(fake_vad(), wake_guard_ms=wake.WAKE_TAIL_MS)
        speech_count = frames_for(wake.MIN_SPEECH_SECONDS)
        speech = [pcm(1000)] * speech_count
        for frame in speech:
            self.assertFalse(recorder.process(frame))
        for _ in range(frames_for(wake.START_TIMEOUT_SECONDS) - speech_count):
            done = recorder.process(pcm())
        self.assertTrue(done)
        self.assertTrue(recorder.audio.startswith(b"".join(speech)))
        self.assertGreater(len(recorder.audio), 0)

    def test_incomplete_frame_is_rejected(self):
        recorder = wake.SpeechRecorder(fake_vad())
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            recorder.process(pcm()[:-2])


class PrerollTests(unittest.TestCase):
    def test_timestamp_keeps_trigger_and_all_buffered_following_audio(self):
        history = [(timestamp, pcm(index))
                   for index, timestamp in enumerate(range(0, 1000, audio.FRAME_MS))]
        selected = wake.select_preroll(history, 400)
        self.assertEqual(selected, [frame for timestamp, frame in history
                                   if timestamp >= 400 - wake.PREROLL_MS])
        self.assertIn(history[-1][1], selected)

    def test_missing_timestamp_uses_bounded_recent_context(self):
        history = [(timestamp, pcm(index))
                   for index, timestamp in enumerate(range(0, 2000, audio.FRAME_MS))]
        selected = wake.select_preroll(history, None)
        self.assertEqual(len(selected), wake.FALLBACK_PREROLL_MS // audio.FRAME_MS)
        self.assertEqual(selected[-1], history[-1][1])


class MicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_selects_only_physical_channel_two(self):
        microphone = wake._Microphone()
        microphone.stream = SimpleNamespace(active=True)
        stereo = struct.pack("<" + "hh" * audio.FRAME_SAMPLES,
                             *([111, 222] * audio.FRAME_SAMPLES))
        microphone.callback(stereo, audio.FRAME_SAMPLES, None, None)
        self.assertEqual(await microphone.next_frame(), pcm(222))

    async def test_overflow_is_bounded_and_rejects_partial_recording(self):
        microphone = wake._Microphone()
        microphone.stream = SimpleNamespace(active=True)
        stereo = struct.pack("<" + "hh" * audio.FRAME_SAMPLES,
                             *([111, 222] * audio.FRAME_SAMPLES))
        for _ in range(microphone.queue.maxsize + 1):
            microphone.callback(stereo, audio.FRAME_SAMPLES, None, None)
        self.assertEqual(microphone.queue.qsize(), microphone.queue.maxsize)
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            await microphone.next_frame()

    async def test_capture_status_and_incomplete_block_fail(self):
        for status, count in [("input overflow", audio.FRAME_SAMPLES),
                              (None, audio.FRAME_SAMPLES - 1)]:
            with self.subTest(status=status, count=count):
                microphone = wake._Microphone()
                microphone.stream = SimpleNamespace(active=True)
                microphone.callback(b"", count, None, status)
                with self.assertRaises(RuntimeError):
                    await microphone.next_frame()

    async def test_closed_stream_does_not_wait_forever(self):
        microphone = wake._Microphone()
        microphone.stream = SimpleNamespace(active=False)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await asyncio.wait_for(microphone.next_frame(), 0.2)

    async def test_active_stream_without_callbacks_times_out(self):
        microphone = wake._Microphone()
        microphone.stream = SimpleNamespace(active=True)
        with patch.object(wake, "AUDIO_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(RuntimeError, "not producing audio"):
                await asyncio.wait_for(microphone.next_frame(), 0.2)


class WakeServiceTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        return client

    def described_models(self, *names):
        return SimpleNamespace(wake=[SimpleNamespace(
            models=[SimpleNamespace(name=name) for name in names])])

    async def test_detected_wake_preserves_trigger_audio_and_captures_command(self):
        client = self.client()
        detection_ready = asyncio.Event()
        read_count = 0
        command = [pcm(1000 + index)
                   for index in range(frames_for(wake.WAKE_TAIL_MS / 1000
                                                + wake.MIN_SPEECH_SECONDS) + 1)]
        context = pcm(7000)
        captured = [context] + command + [pcm()] * frames_for(wake.END_SILENCE_SECONDS)

        async def read_event():
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return Event(type="info", data={})
            await detection_ready.wait()
            return Detection(name="athena", timestamp=audio.FRAME_MS).event()

        async def write_event(event):
            if event.type == "audio-chunk":
                detection_ready.set()

        client.read_event.side_effect = read_event
        client.write_event.side_effect = write_event
        stream = MagicMock()
        stream.active = True

        def open_stream(**settings):
            self.assertEqual(settings["channels"], 2)
            self.assertEqual(settings["samplerate"], 16000)
            self.assertEqual(settings["blocksize"], audio.FRAME_SAMPLES)

            def begin():
                for frame in captured:
                    samples = struct.unpack("<" + "h" * audio.FRAME_SAMPLES, frame)
                    interleaved = [sample for value in samples for sample in (111, value)]
                    stereo = struct.pack("<" + "h" * len(interleaved), *interleaved)
                    settings["callback"](stereo, audio.FRAME_SAMPLES, None, None)
                return stream

            stream.__enter__.side_effect = begin
            return stream

        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.AsyncClient, "from_uri", return_value=client), \
             patch.object(wake.Info, "from_event", return_value=self.described_models("athena")), \
             patch.object(wake.sd, "RawInputStream", side_effect=open_stream), \
             patch.object(wake.webrtcvad, "Vad", return_value=fake_vad()), \
             redirect_stdout(io.StringIO()):
            result = await asyncio.wait_for(wake._record(settings), 0.5)
        self.assertEqual(result, b"".join(captured))
        events = [call.args[0] for call in client.write_event.call_args_list]
        self.assertEqual([event.type for event in events[:3]],
                         ["describe", "detect", "audio-start"])
        self.assertEqual(events[1].data["names"], ["athena"])
        self.assertEqual(events[3].payload, context)
        self.assertEqual(events[3].data["timestamp"], 0)
        stream.__exit__.assert_called_once()
        client.__aexit__.assert_awaited_once()

    async def test_unavailable_selected_model_fails_before_opening_microphone(self):
        client = self.client()
        client.read_event.return_value = Event(type="info", data={})
        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.AsyncClient, "from_uri", return_value=client), \
             patch.object(wake.Info, "from_event", return_value=self.described_models("hey_jarvis")), \
             patch.object(wake.sd, "RawInputStream") as microphone:
            with self.assertRaisesRegex(wake.WakeSetupError, "athena.*not installed"):
                await wake._record(settings)
        microphone.assert_not_called()
        client.__aexit__.assert_awaited_once()
        self.assertEqual([call.args[0].type for call in client.write_event.call_args_list],
                         ["describe"])

    async def test_advertised_but_uninstalled_model_is_rejected(self):
        client = self.client()
        client.read_event.return_value = Event(type="info", data={})
        models = SimpleNamespace(wake=[SimpleNamespace(models=[
            SimpleNamespace(name="athena", installed=False)])])
        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.Info, "from_event", return_value=models):
            with self.assertRaisesRegex(wake.WakeSetupError, "athena.*not installed"):
                await wake._validate_model(client, settings)

    async def test_malformed_model_schema_is_a_fatal_setup_error(self):
        client = self.client()
        client.read_event.return_value = Event(type="info", data={})
        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.Info, "from_event", side_effect=ValueError("bad schema")):
            with self.assertRaisesRegex(wake.WakeSetupError, "invalid model information"):
                await wake._validate_model(client, settings)

    async def test_service_eof_without_microphone_frames_closes_stream(self):
        client = self.client()
        client.read_event.side_effect = [Event(type="info", data={}), None]
        stream = MagicMock()
        stream.active = True
        stream.__enter__.return_value = stream
        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.AsyncClient, "from_uri", return_value=client), \
             patch.object(wake.Info, "from_event", return_value=self.described_models("athena")), \
             patch.object(wake.sd, "RawInputStream", return_value=stream), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "disconnected.*listening"):
                await asyncio.wait_for(wake._record(settings), 0.5)
        stream.__exit__.assert_called_once()
        client.__aexit__.assert_awaited_once()
        events = [call.args[0] for call in client.write_event.call_args_list]
        self.assertEqual([event.type for event in events], ["describe", "detect", "audio-start"])
        self.assertEqual(events[1].data["names"], ["athena"])
        self.assertEqual(events[2].data["channels"], 1)
        self.assertEqual(events[2].data["rate"], 16000)

    async def test_cancellation_closes_idle_microphone_and_client(self):
        client = self.client()
        reading = asyncio.Event()
        opened = asyncio.Event()
        read_count = 0

        async def read_event():
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return Event(type="info", data={})
            reading.set()
            await asyncio.Event().wait()

        client.read_event.side_effect = read_event
        stream = MagicMock()
        stream.active = True
        stream.__enter__.side_effect = lambda: (opened.set(), stream)[1]
        settings = wake.WakeSettings("tcp://localhost:10400", "athena", "Athena")
        with patch.object(wake.AsyncClient, "from_uri", return_value=client), \
             patch.object(wake.Info, "from_event", return_value=self.described_models("athena")), \
             patch.object(wake.sd, "RawInputStream", return_value=stream), \
             redirect_stdout(io.StringIO()):
            recording = asyncio.create_task(wake._record(settings))
            await asyncio.wait_for(opened.wait(), 0.5)
            await asyncio.wait_for(reading.wait(), 0.5)
            recording.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await recording
        stream.__exit__.assert_called_once()
        client.__aexit__.assert_awaited_once()

    async def test_unrelated_model_detection_does_not_activate_selected_model(self):
        client = self.client()
        client.read_event.side_effect = [
            Detection(name="hey_jarvis", timestamp=20).event(),
            Detection(name="athena", timestamp=40).event(),
        ]
        result = await wake._read_detection(client, "athena")
        self.assertEqual(result.name, "athena")
        self.assertEqual(result.timestamp, 40)

    async def test_malformed_detection_is_a_recoverable_service_error(self):
        client = self.client()
        client.read_event.return_value = Event(type="detection", data={})
        with patch.object(wake.Detection, "from_event", return_value=SimpleNamespace(
                name="athena", timestamp="not a timestamp")):
            with self.assertRaisesRegex(RuntimeError, "invalid detection"):
                await wake._read_detection(client, "athena")


if __name__ == "__main__":
    unittest.main()
