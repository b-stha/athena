"""Exercise actual OVOS buffering without a microphone or transcription server."""

import time
import unittest
from unittest.mock import Mock

from ovos_plugin_manager.templates.stt import STT
from ovos_utils import classproperty

from athena_ovos.streaming import BufferedWhisperSTT


class RecordedSTT(STT):
    """Local STT stub retaining the real base-class transcribe adapter."""

    def __init__(self):
        super().__init__({"lang": "en-US"})
        self.recordings = []

    @classproperty
    def available_languages(cls):
        return {"en-US"}

    def execute(self, audio, language=None):
        self.recordings.append((audio.get_raw_data(), language))
        return "Fake acknowledgement."


class BufferedSTTShutdownTests(unittest.TestCase):
    def setUp(self):
        self.engine = RecordedSTT()
        self.engine.execute = Mock(wraps=self.engine.execute)
        self.engine.transcribe = Mock(wraps=self.engine.transcribe)
        self.stt = BufferedWhisperSTT(self.engine, {"lang": "en-US"})
        self.addCleanup(self.stt.shutdown)

    def recording(self, data=bytes(640)):
        self.stt.stream_start("en-US")
        worker = self.stt.stream
        self.stt.stream_data(data)
        deadline = time.monotonic() + 1
        while len(worker.buffer) < len(data):
            if not worker.is_alive() or time.monotonic() >= deadline:
                self.fail("Real OVOS worker did not buffer the recording.")
            time.sleep(0.005)
        return worker

    def test_shutdown_discards_partial_audio_and_joins_worker_without_transcribing(self):
        worker = self.recording()
        self.assertTrue(worker.is_alive())
        self.stt.shutdown()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker.buffer), 0)
        self.assertEqual(worker.buffer.read(), b"")
        self.assertIsNone(self.stt.stream)
        self.assertIsNone(self.stt.queue)
        self.assertTrue(self.stt.transcript_ready.is_set())
        self.engine.execute.assert_not_called()
        self.engine.transcribe.assert_not_called()

    def test_shutdown_without_any_stream_is_idempotent_and_does_not_transcribe(self):
        self.stt.shutdown()
        self.stt.shutdown()
        self.assertIsNone(self.stt.stream)
        self.engine.execute.assert_not_called()
        self.engine.transcribe.assert_not_called()

    def test_repeated_shutdown_of_empty_running_stream_leaves_no_worker(self):
        self.stt.stream_start("en-US")
        worker = self.stt.stream
        self.stt.shutdown()
        self.stt.shutdown()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker.buffer), 0)
        self.engine.execute.assert_not_called()
        self.engine.transcribe.assert_not_called()

    def test_completed_recording_still_transcribes_exactly_once(self):
        data = b"\x01\x00" * 320
        worker = self.recording(data)
        self.assertEqual(self.stt.transcribe(lang="en-US"), [("Fake acknowledgement.", 1.0)])
        self.stt.stream_stop()
        self.stt.shutdown()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker.buffer), 0)
        self.engine.execute.assert_called_once()
        self.engine.transcribe.assert_called_once()
        self.assertEqual(self.engine.recordings, [(data, "en-US")])

    def test_aborted_session_is_not_replayed_into_the_next_completed_session(self):
        first = self.recording(b"\x01\x00" * 320)
        self.stt.shutdown()
        second_data = b"\x02\x00" * 320
        second = self.recording(second_data)
        self.assertIsNot(second, first)
        self.assertEqual(self.stt.transcribe(lang="en-US"), [("Fake acknowledgement.", 1.0)])
        self.stt.stream_stop()
        self.stt.shutdown()
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(first.buffer), 0)
        self.assertEqual(len(second.buffer), 0)
        self.engine.execute.assert_called_once()
        self.assertEqual(self.engine.recordings, [(second_data, "en-US")])

    def test_two_aborted_sessions_leave_no_threads_or_transcription_requests(self):
        workers = []
        for sample in [b"\x01\x00", b"\x02\x00"]:
            workers.append(self.recording(sample * 320))
            self.stt.shutdown()
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertTrue(all(len(worker.buffer) == 0 for worker in workers))
        self.engine.execute.assert_not_called()
        self.engine.transcribe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
