import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from router import route, voice_targets
from resolver import normalize, resolve


class ResolverTests(unittest.TestCase):
    def test_notepad_variants(self):
        for phrase in ["Open no pad.", "open node pad", "open note pad", "OPEN NOTEPAD",
                       "open no path", "open no padded", "open no pass"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve(phrase, voice_targets()), "open notepad")

    def test_punctuation_between_action_and_target(self):
        transcript = "Turn off, Nanoleafs."
        self.assertEqual(normalize(transcript), "turn off nanoleafs")
        with patch("router.home_assistant.call_service") as service:
            route(resolve(transcript, voice_targets()))
        service.assert_called_once_with("light", "turn_off", "light.nanoleafs")

    def test_device_transcription_variants(self):
        for phrase, expected in [("turn on bath rum", "turn on bathroom"),
                                 ("turn off nanoleafes", "turn off nanoleafs"),
                                 ("turn on my pea sea", "turn on my pc")]:
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve(phrase, voice_targets()), expected)

    def test_fuzzy_matching_does_not_require_transcription_aliases(self):
        self.assertEqual(resolve("open no padded", {"open": {"notepad": "notepad"}}),
                         "open notepad")

    def test_rejects_bad_actions_and_targets(self):
        for phrase in ["opn notepad", "don't open notepad", "close notepad", "turn of bedroom",
                       "open pad", "open calculator", "open notepad and shutdown", "open"]:
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                resolve(phrase, voice_targets())

    def test_ambiguous_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve("open node pad", {"open": {"notepad": "notepad", "notepod": "notepod"}})

    def test_similar_room_names_are_not_guessed(self):
        with self.assertRaises(ValueError):
            resolve("turn on bed roomm", {"turn on": {"bedroom": "bedroom", "bedrooms": "bedrooms"}})

    def test_spacing_exact_names_and_aliases(self):
        for phrase, expected in [("turn off bed room", "turn off bedroom"),
                                 ("turn on bathroom lights", "turn on bathroom"),
                                 ("turn on my PC", "turn on my pc"), ("", ""), ("Exit.", "exit")]:
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve(phrase, voice_targets()), expected)

    def test_resolver_is_silent_and_routes_canonical_command(self):
        output = io.StringIO()
        with patch("router.desktop.execute") as execute, redirect_stdout(output):
            command = resolve("Open no pad.", voice_targets())
            route(command)
        execute.assert_called_once_with("launch_app", "notepad")
        self.assertEqual(output.getvalue(), "")
