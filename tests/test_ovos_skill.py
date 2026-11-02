"""Legacy structured actions and the new transcript-to-agent boundary."""

import unittest
from unittest.mock import Mock, patch

import requests
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


class OVOSAgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.skill = AthenaSkill.__new__(AthenaSkill)
        self.skill._bus = FakeBus()
        self.addCleanup(self.skill.bus.close)
        self.skill.log = Mock()
        self.skill.speak = Mock()
        self.skill.register_fallback = Mock()
        self.skill.register_intent = Mock()
        self.events = []
        for topic in [ACTION_RESULT, CONTEXT_REQUEST]:
            self.skill.bus.on(topic, self.events.append)
        self.worker = Mock()
        self.worker.submit.return_value = True
        with patch("athena_agent.worker.AgentWorker", return_value=self.worker):
            self.skill.initialize()

    def test_every_transcript_goes_to_agent_without_language_parsing(self):
        for text in ["sleep pc", "do not sleep pc", "put that over there", "turn off lights and open notepad"]:
            with self.subTest(text=text), patch("router.desktop.execute") as desktop, patch("router.home_assistant.call_service") as home:
                self.events.clear()
                message = Message("fallback", {"utterances": [text]}, {"session": {"session_id": "room"}})
                self.assertTrue(self.skill.handle_context(message))
                args = self.worker.submit.call_args
                self.assertEqual(args.args[0], text)
                self.assertEqual(args.kwargs["session_id"], "room")
                self.assertEqual(self.events[0].data, {"utterance": text, "reason": "agent_request"})
                desktop.assert_not_called()
                home.assert_not_called()
        self.skill.register_intent.assert_not_called()
        self.skill.register_fallback.assert_called_once_with(self.skill.handle_context, 91)

    def test_response_and_error_callbacks_preserve_message_context(self):
        message = Message("fallback", {"utterance": "hello"}, {"session": {"session_id": "room"}})
        self.skill.handle_context(message)
        self.worker.submit.call_args.args[1]("Agent response")
        self.assertEqual(self.events[-1].data, {"status": "agent_response", "response": "Agent response"})
        self.assertEqual(self.events[-1].context["session"]["session_id"], "room")
        self.skill.speak.assert_called_with("Agent response")
        self.worker.submit.call_args.args[2]("Configuration failed")
        self.assertEqual(self.events[-1].data["status"], "failed")

    def test_empty_input_is_not_enqueued_and_busy_is_reported(self):
        for data in [{}, {"utterance": None}, {"utterances": "sleep pc"}, {"utterances": [42]}, {"utterance": "   "}]:
            message = Message("fallback", data)
            self.assertFalse(self.skill.can_answer(message))
            self.assertFalse(self.skill.handle_context(message))
        self.worker.submit.assert_not_called()
        self.worker.submit.return_value = False
        self.skill.handle_context(Message("fallback", {"utterance": "hello"}))
        self.assertIn("still handling", self.skill.speak.call_args.args[0])

    def test_shutdown_closes_owned_worker(self):
        self.skill.shutdown()
        self.worker.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
