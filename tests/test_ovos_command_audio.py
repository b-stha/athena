"""Check phrase boundaries and queued audio using real OVOS buffering."""
import struct
import threading
import unittest
from unittest.mock import Mock

from ovos_plugin_manager.templates.stt import STT
from ovos_vad_plugin_webrtcvad import WebRTCVAD

from athena_ovos.listener import AthenaVoiceLoop, _command_speech
from athena_ovos.plugins import AthenaMicrophone
from athena_ovos.streaming import BufferedWhisperSTT


class ClassifiedFrames:
    """Deterministic frame labels; these tests do not measure acoustic accuracy."""
    def is_speech(self, frame, sample_rate):
        return struct.unpack('<h', frame[:2])[0] != 0


def vad():
    engine = WebRTCVAD({'frame_duration_ms': 20, 'vad_mode': 2}, sample_rate=16000)
    engine.vad = ClassifiedFrames()
    return engine


def pcm(value, seconds):
    return struct.pack('<h', value) * round(16000 * seconds)


class RecordedSTT(STT):
    def __init__(self):
        super().__init__({'lang': 'en-US'})
        self.recordings = []
        self.called = threading.Event()

    def execute(self, audio, language=None):
        self.recordings.append(audio.get_raw_data())
        self.called.set()
        return 'Recorded command.'


class CommandAudioTests(unittest.TestCase):
    def setUp(self):
        self.first = pcm(1200, 1.4)
        self.pause = pcm(0, 0.8)
        self.second = pcm(2400, 1.4)
        self.audio = pcm(0, 0.4) + self.first + self.pause + self.second + pcm(0, 2)

    def test_preserves_later_phrase_that_upstream_extraction_discards(self):
        native = vad().extract_speech(self.audio)
        self.assertIn(pcm(1200, 0.1), native)
        self.assertNotIn(pcm(2400, 0.1), native)
        retained = _command_speech(self.audio, vad())
        self.assertIn(pcm(1200, 0.1), retained)
        self.assertIn(self.pause, retained)
        self.assertIn(self.second, retained)
        self.assertIn(retained, self.audio)  # One continuous slice, no splicing.

    def test_single_phrase_retains_the_existing_padding_and_trim(self):
        audio = pcm(0, 0.4) + self.first + pcm(0, 2)
        self.assertEqual(_command_speech(audio, vad()), vad().extract_speech(audio))
        self.assertLess(len(_command_speech(audio, vad())), len(audio))

    def test_retains_last_phrase_even_without_a_final_silence_segment(self):
        retained = _command_speech(pcm(0, 0.4) + self.first + self.pause + self.second, vad())
        self.assertTrue(retained.endswith(self.second))

    def test_uncertain_or_incomplete_audio_declines_trimming(self):
        for audio in (b'', pcm(0, 3), self.audio + b'\x00'):
            with self.subTest(length=len(audio)):
                self.assertIsNone(_command_speech(audio, vad()))

    def test_twenty_ms_capture_preserves_a_partial_thirty_ms_vad_frame(self):
        engine = WebRTCVAD({'frame_duration_ms': 30, 'vad_mode': 2}, sample_rate=16000)
        engine.vad = ClassifiedFrames()
        audio = pcm(0, 0.4) + self.first + self.pause + self.second
        self.assertNotEqual(len(audio) % 960, 0)
        retained = _command_speech(audio, engine)
        self.assertIsNotNone(retained)
        self.assertTrue(retained.endswith(self.second))
        self.assertIn(retained, audio)

    def test_classifier_state_is_reset_before_and_after_extraction(self):
        engine = vad()
        for _ in range(20):
            engine.is_silence(pcm(1200, 0.02))
        audio = pcm(0, 0.4) + self.first + self.pause + self.second
        self.assertEqual(_command_speech(audio, engine), _command_speech(audio, vad()))
        # OVOS releases keep different internal VAD fields. Verify the next
        # classification instead: preceding speech must not carry into silence.
        self.assertTrue(engine.is_silence(pcm(0, 0.02)))


class BufferedCommandTests(unittest.TestCase):
    def setUp(self):
        self.engine = RecordedSTT()
        self.stt = BufferedWhisperSTT(self.engine, {'lang': 'en-US'})
        self.addCleanup(self.stt.shutdown)

    def test_transcription_waits_for_the_queued_final_audio(self):
        self.stt.stream_start('en-US')
        worker = self.stt.stream
        original = worker.update
        blocked, release = threading.Event(), threading.Event()
        first, second = pcm(1200, 0.02), pcm(2400, 0.02)

        def delayed(data):
            if data == second:
                blocked.set()
                if not release.wait(2):
                    raise RuntimeError('Test did not release the final frame')
            original(data)
        worker.update = delayed
        self.stt.stream_data(first)
        self.stt.stream_data(second)
        self.assertTrue(blocked.wait(1))
        results = []
        request = threading.Thread(target=lambda: results.append(self.stt.transcribe(lang='en-US')))
        request.start()
        try:
            self.assertFalse(self.engine.called.wait(0.05))
        finally:
            release.set()
            request.join(3)
        self.assertFalse(request.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [[('Recorded command.', 1.0)]])
        self.assertEqual(self.engine.recordings, [first + second])

    def test_draining_the_buffer_alone_never_transcribes_or_discards_audio(self):
        audio = pcm(1200, 0.2)
        self.stt.stream_start('en-US')
        self.stt.stream_data(audio)
        self.stt.finish_buffering()
        self.stt.finish_buffering()
        self.assertFalse(self.stt.stream.is_alive())
        self.assertFalse(self.engine.recordings)
        self.stt.transcribe(lang='en-US')
        self.assertEqual(self.engine.recordings, [audio])

    def test_native_listener_passes_both_phrases_to_stt_once(self):
        audio = pcm(0, 0.4) + pcm(1200, 1.4) + pcm(0, 0.8) + pcm(2400, 1.4) + pcm(0, 2)
        self.stt.stream_start('en-US')
        for offset in range(0, len(audio), 640):
            self.stt.stream_data(audio[offset:offset + 640])
        loop = AthenaVoiceLoop(mic=AthenaMicrophone(), hotwords=Mock(), stt=self.stt,
            fallback_stt=None, vad=vad(), transformers=Mock())
        loop._speech_confirmed = True
        loop.stt_audio_bytes = audio
        loop._vad_remove_silence()
        self.stt.transcribe(lang='en-US')
        self.assertEqual(len(self.engine.recordings), 1)
        self.assertIn(pcm(2400, 1.4), self.engine.recordings[0])
        self.assertIn(pcm(0, 0.8), self.engine.recordings[0])


if __name__ == '__main__':
    unittest.main()
