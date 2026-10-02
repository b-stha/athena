"""Exercise real Adapt matching with injected backend calls and no microphone."""

import unittest
from unittest.mock import Mock, patch

import requests
from ovos_adapt.engine import IntentDeterminationEngine
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from athena_skill import ACTION_RESULT, CONTEXT_REQUEST, AthenaSkill
from router import Action, UnknownCommand, execute_action, parse_action


class StructuredActionTests(unittest.TestCase):
    def test_parsing_is_pure_and_aliases_are_independent_of_actions(self):
        with patch("router.desktop.execute") as desktop, patch("router.home_assistant.call_service") as home:
            for verb, action in [("sleep", "sleep"), ("restart", "restart"), ("reboot", "restart"),
                                 ("shut down", "shutdown"), ("wake", "wake")]:
                for target in ["pc", "my pc", "computer", "my computer", "desktop", "the desktop"]:
                    with self.subTest(verb=verb, target=target):
                        self.assertEqual(parse_action(f"{verb} {target}"),
                                         Action("desktop", action, "pc", "device"))
            self.assertEqual(parse_action("Please put my computer to sleep."),
                             Action("desktop", "sleep", "pc", "device"))
            self.assertEqual(parse_action("send the desktop to sleep please"),
                             Action("desktop", "sleep", "pc", "device"))
            self.assertEqual(parse_action("launch notepad"), Action("desktop", "launch_app", "notepad", "application"))
            self.assertEqual(parse_action("switch off bedroom lights"),
                             Action("home_assistant", "turn_off", "bedroom", "area_id"))
            desktop.assert_not_called()
            home.assert_not_called()

    def test_negation_context_questions_and_multiple_actions_cannot_execute(self):
        utterances = ["don't sleep my pc", "do not sleep pc", "never restart pc",
                      "sleep pc and restart pc", "turn on bedroom and bathroom",
                      "can you sleep pc", "if it is late sleep pc", "sleep that",
                      "put it to sleep", "sleep bathroom", "open notepad tomorrow",
                      "open notepad and shutdown pc"]
        with patch("router.desktop.execute") as desktop, patch("router.home_assistant.call_service") as home:
            for utterance in utterances:
                with self.subTest(utterance=utterance), self.assertRaises(UnknownCommand):
                    parse_action(utterance)
            desktop.assert_not_called()
            home.assert_not_called()

    def test_forged_structured_actions_are_rejected_before_io(self):
        actions = [Action("shell", "execute", "notepad"),
                   Action("desktop", "shutdown", "bathroom", "device"),
                   Action("desktop", "sleep", "pc", "application"),
                   Action("desktop", "launch_app", "powershell", "application"),
                   Action("home_assistant", "turn_on", "light.unknown"),
                   Action("home_assistant", "delete", "light.nanoleafs"),
                   Action("home_assistant", "turn_on", "bedroom", "entity_id"), None]
        with patch("router.desktop.execute") as desktop, patch("router.home_assistant.call_service") as home:
            for action in actions:
                with self.subTest(action=action), self.assertRaises(ValueError):
                    execute_action(action)
            desktop.assert_not_called()
            home.assert_not_called()


class OVOSIntentTests(unittest.TestCase):
    def setUp(self):
        # Keep the real Skill methods, Message and Adapt parser. Skip framework
        # startup to avoid settings files, websocket threads and audio output.
        self.skill = AthenaSkill.__new__(AthenaSkill)
        self.skill._bus = FakeBus()
        self.skill.log = Mock()
        self.skill.speak = Mock()
        self.skill.default_shutdown = Mock()
        self.events = []
        for topic in [ACTION_RESULT, CONTEXT_REQUEST]:
            self.skill.bus.on(topic, self.events.append)
        self.engine = IntentDeterminationEngine()
        self.skill.register_vocabulary = Mock(
            side_effect=lambda word, entity, **kwargs: self.engine.register_entity(word, entity))
        self.skill.register_intent = Mock(
            side_effect=lambda builder, handler: self.engine.register_intent_parser(builder.build()))
        self.skill.register_fallback = Mock()
        self.skill.initialize()

    def dispatch(self, utterance):
        matches = list(self.engine.determine_intent(utterance))
        if not matches:
            self.skill.handle_context(Message("fallback", {"utterances": [utterance]}))
            return
        message = Message(matches[0]["intent_type"], matches[0] | {"utterance": utterance},
                          {"session": {"session_id": "test-session"}})
        self.skill.handle_command(message)

    def test_framework_matches_power_variants_and_dispatches_only_once(self):
        for phrase, expected in [("sleep pc", "sleep"), ("put my computer to sleep", "sleep"),
                                 ("send the desktop to sleep", "sleep"), ("reboot my desktop", "restart"),
                                 ("turn off the computer", "shutdown"), ("wake up my pc", "wake")]:
            with self.subTest(phrase=phrase), patch("router.desktop.execute") as execute, \
                 patch("router.home_assistant.call_service") as home:
                self.events.clear()
                self.assertTrue(list(self.engine.determine_intent(phrase)))
                self.dispatch(phrase)
                execute.assert_called_once_with(expected, "pc")
                home.assert_not_called()
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0].msg_type, ACTION_RESULT)
                self.assertTrue(self.events[0].data["success"])
                self.assertNotIn("session", self.events[0].data)
                self.assertEqual(self.events[0].context["session"]["session_id"], "test-session")

    def test_framework_keeps_home_and_desktop_backends_separate(self):
        with patch("router.home_assistant.call_service") as home, patch("router.desktop.execute") as desktop:
            self.dispatch("switch off bedroom lights")
            home.assert_called_once_with("light", "turn_off", area_id="bedroom")
            desktop.assert_not_called()
        with patch("router.home_assistant.call_service") as home, patch("router.desktop.execute") as desktop:
            self.dispatch("open notepad")
            desktop.assert_called_once_with("launch_app", "notepad")
            home.assert_not_called()

    def test_keyword_false_positives_never_execute_negated_or_ambiguous_commands(self):
        # Adapt may find positive keywords inside a negative sentence. The
        # complete grammar must reject it at the handler boundary.
        for utterance in ["don't sleep pc", "do not reboot my pc", "sleep pc and shutdown pc",
                          "turn off bedroom and bathroom", "what happens when I sleep pc"]:
            with self.subTest(utterance=utterance), patch("router.desktop.execute") as desktop, \
                 patch("router.home_assistant.call_service") as home:
                self.events.clear()
                self.dispatch(utterance)
                desktop.assert_not_called()
                home.assert_not_called()
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0].msg_type, CONTEXT_REQUEST)

    def test_unknown_contextual_request_uses_one_boundary_and_never_a_backend(self):
        with patch("router.desktop.execute") as desktop, patch("router.home_assistant.call_service") as home:
            self.dispatch("put that to sleep")
            desktop.assert_not_called()
            home.assert_not_called()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].data,
                         {"utterance": "put that to sleep", "reason": "no_deterministic_match"})
        self.skill.register_fallback.assert_called_once_with(self.skill.handle_context, 91)

    def test_backend_failure_is_reported_without_fallback_or_retry(self):
        with patch("router.desktop.execute", side_effect=requests.Timeout("timeout")) as execute:
            self.dispatch("sleep pc")
            execute.assert_called_once_with("sleep", "pc")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].msg_type, ACTION_RESULT)
        self.assertFalse(self.events[0].data["success"])
        self.assertNotIn("accepted", self.skill.speak.call_args.args[0].lower())

    def test_failed_backend_response_is_not_acknowledged(self):
        with patch("router.desktop.execute", side_effect=ValueError("Sleep rejected.")) as execute:
            self.dispatch("sleep pc")
            execute.assert_called_once_with("sleep", "pc")
        self.assertEqual(self.events[0].data["error"], "Sleep rejected.")
        self.assertFalse(self.events[0].data["success"])
        self.assertNotIn("accepted", self.skill.speak.call_args.args[0].lower())

    def test_power_ack_and_wake_confirmation_are_distinct(self):
        for action in ["shutdown", "restart", "sleep", "wake"]:
            with self.subTest(action=action), patch("router.desktop.execute"):
                self.dispatch(f"{action} pc")
            response = self.skill.speak.call_args.args[0]
            if action == "wake":
                self.assertIn("not confirmed", response)
                self.assertNotIn("accepted", response)
            else:
                self.assertIn(f"{action.capitalize()} accepted by the desktop client.", response)

    def test_intent_slots_cannot_override_original_sentence(self):
        message = Message("SleepDesktop", {"utterance": "don't sleep pc",
                                           "DesktopTarget": "pc", "SleepDesktopVerb": "sleep"})
        with patch("router.desktop.execute") as execute:
            self.skill.handle_command(message)
            execute.assert_not_called()
        self.assertEqual(self.events[0].msg_type, CONTEXT_REQUEST)

    def test_empty_or_malformed_input_is_not_handled(self):
        for data in [{}, {"utterance": None}, {"utterances": "sleep pc"}, {"utterances": [42]},
                     {"utterance": "   "}]:
            with self.subTest(data=data):
                message = Message("fallback", data)
                self.assertFalse(self.skill.can_answer(message))
                self.assertFalse(self.skill.handle_context(message))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
