import struct
import unittest

from athena_wake.runtime import BufferedWakeSession, FRAME_BYTES, PcmRing, State, ThresholdTrigger


class Detector:
    def __init__(self):
        self.score = None
        self.audio = []

    def predict(self, pcm):
        self.audio.append(pcm)
        return self.score


class BufferTests(unittest.TestCase):
    def test_sample_exact_preroll_and_oversized_input(self):
        ring = PcmRing(4 / 16000)
        ring.append(struct.pack("<3h", 1, 2, 3))
        ring.append(struct.pack("<3h", 4, 5, 6))
        self.assertEqual(ring.snapshot(), struct.pack("<4h", 3, 4, 5, 6))
        ring.append(struct.pack("<6h", 7, 8, 9, 10, 11, 12))
        self.assertEqual(ring.snapshot(), struct.pack("<4h", 9, 10, 11, 12))
        with self.assertRaises(ValueError):
            ring.append(b"x")
        self.assertEqual(ring.snapshot(), struct.pack("<4h", 9, 10, 11, 12))
        ring.clear()
        self.assertEqual(ring.snapshot(), b"")

    def test_only_fresh_predictions_count_and_below_threshold_resets_streak(self):
        trigger = ThresholdTrigger(0.5, 2, 0)
        self.assertFalse(trigger.update(.9, 320))
        self.assertFalse(trigger.update(None, 640))
        self.assertTrue(trigger.update(.8, 960))
        self.assertFalse(trigger.update(.9, 1280))
        self.assertFalse(trigger.update(.4, 1600))
        self.assertFalse(trigger.update(.9, 1920))
        self.assertTrue(trigger.update(.9, 2240))

    def test_cooldown_boundary_is_audio_time(self):
        trigger = ThresholdTrigger(cooldown=.08)
        self.assertTrue(trigger.update(.9, 1280))
        self.assertFalse(trigger.update(.9, 2559))
        self.assertTrue(trigger.update(.9, 2560))
        with self.assertRaises(ValueError):
            trigger.update(.9, 100)

    def test_invalid_settings_and_scores(self):
        for options in ({"threshold": True}, {"threshold": float("nan")},
                        {"threshold": 1.1}, {"consecutive": True},
                        {"consecutive": 0}, {"cooldown": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ThresholdTrigger(**options)
        trigger = ThresholdTrigger()
        for score in (True, float("inf"), -1, 2):
            with self.subTest(score=score), self.assertRaises(ValueError):
                trigger.update(score, 0)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.detector = Detector()

    def frame(self, value):
        return struct.pack("<320h", *([value] * 320))

    def test_candidate_and_incoming_handoff_have_no_loss_or_duplicate_trigger_frame(self):
        session = BufferedWakeSession(self.detector, preroll=.06, silence_end=.04)
        frames = [self.frame(n) for n in range(1, 7)]
        for frame in frames[:3]:
            session.feed(frame, speech=False)
        self.detector.score = .9
        events = session.feed(frames[3], speech=True)
        candidate = events[-1]
        self.assertEqual(candidate.kind, "candidate")
        self.assertEqual(candidate.audio, b"".join(frames[1:4]))
        self.assertEqual(candidate.start_sample, 320)
        self.detector.score = None
        streamed = []
        for frame in frames[4:]:
            streamed.extend(session.feed(frame, speech=True))
        for _ in range(2):
            streamed.extend(session.feed(bytes(FRAME_BYTES), speech=False))
        command = next(event for event in streamed if event.kind == "command")
        handed_off = candidate.audio + b"".join(event.audio for event in streamed if event.kind == "audio")
        self.assertEqual(command.audio, handed_off)
        self.assertEqual(command.reason, "silence")
        self.assertEqual(session.state, State.COOLDOWN)
        self.assertEqual(len(self.detector.audio), 8)

    def test_verification_can_arrive_after_command_audio_and_rejection_discards(self):
        session = BufferedWakeSession(self.detector, verify=True, cooldown=.04)
        self.detector.score = .9
        candidate = session.feed(self.frame(1), speech=False)[0]
        incoming = session.feed(self.frame(2), speech=True)
        self.assertEqual(session.state, State.VERIFYING)
        self.assertEqual(incoming[0].audio, self.frame(2))
        self.assertEqual(session.resolve_verification(False)[0].kind, "rejected")
        self.assertEqual(session._audio, b"")
        self.assertEqual(session.ring.snapshot(), b"")
        session.feed(self.frame(3), speech=False)
        events = session.feed(self.frame(4), speech=False)
        self.assertEqual([event.kind for event in events], ["idle", "candidate"])
        self.assertEqual(events[-1].audio, self.frame(3) + self.frame(4))
        self.assertNotIn(candidate.audio, events[-1].audio)

    def test_verification_acceptance_keeps_early_command_audio(self):
        session = BufferedWakeSession(self.detector, verify=True, silence_end=.02)
        self.detector.score = .9
        session.feed(self.frame(1), speech=False)
        session.feed(self.frame(2), speech=True)
        self.assertEqual(session.resolve_verification(True)[0].kind, "verified")
        events = session.feed(self.frame(3), speech=False)
        self.assertEqual(events[-1].audio, self.frame(1) + self.frame(2) + self.frame(3))
        self.assertEqual(events[-1].kind, "command")

    def test_no_post_trigger_speech_preserves_late_detected_immediate_command(self):
        session = BufferedWakeSession(self.detector, speech_timeout=.04)
        session.feed(self.frame(7), speech=True)
        self.detector.score = .9
        session.feed(self.frame(8), speech=True)
        self.detector.score = None
        session.feed(self.frame(0), speech=False)
        event = session.feed(self.frame(0), speech=False)[-1]
        self.assertEqual(event.kind, "command")
        self.assertEqual(event.reason, "speech_timeout")
        self.assertTrue(event.audio.startswith(self.frame(7) + self.frame(8)))

    def test_timeouts_bound_audio_and_verification(self):
        for verify in (False, True):
            with self.subTest(verify=verify):
                session = BufferedWakeSession(self.detector, verify=verify,
                                              max_command=.04, verification_timeout=.02)
                self.detector.score = .9
                session.feed(self.frame(1), speech=False)
                events = session.feed(self.frame(2), speech=True)
                if verify:
                    self.assertEqual(events[-1].reason, "verification_timeout")
                    self.assertEqual(events[-1].kind, "rejected")
                else:
                    events = session.feed(self.frame(3), speech=True)
                    self.assertEqual(events[-1].reason, "recording_timeout")
                    self.assertEqual(len(events[-1].audio), 3 * FRAME_BYTES)
                self.assertEqual(session.state, State.COOLDOWN)
                with self.assertRaises(RuntimeError):
                    session.resolve_verification(True)

    def test_close_cancels_without_final_command_and_is_idempotent(self):
        session = BufferedWakeSession(self.detector)
        self.detector.score = .9
        session.feed(self.frame(1), speech=False)
        self.assertEqual([event.kind for event in session.close()], ["cancelled"])
        self.assertEqual(session.close(), [])
        self.assertEqual(session._audio, b"")
        with self.assertRaises(RuntimeError):
            session.feed(self.frame(1), speech=True)

    def test_invalid_frame_does_not_change_sample_clock(self):
        session = BufferedWakeSession(self.detector)
        with self.assertRaises(ValueError):
            session.feed(b"\0\0", speech=False)
        self.assertEqual(session.sample, 0)
        self.assertEqual(self.detector.audio, [])


if __name__ == "__main__":
    unittest.main()
