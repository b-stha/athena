import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import main
from integrations import desktop
from resolver import resolve
from router import route, voice_targets


class PowerCommandTests(unittest.TestCase):
    phrases = {
        "shutdown": ["shutdown PC", "shutdown my PC", "shut down PC",
                     "shut down my PC", "turn off PC", "turn off my PC"],
        "restart": ["restart PC", "restart my PC"],
        "sleep": ["sleep PC", "sleep my PC"],
    }

    def test_power_phrases_use_http_without_waking(self):
        for action, phrases in self.phrases.items():
            for phrase in phrases:
                for mode in ["text", "voice"]:
                    with self.subTest(phrase=phrase, mode=mode), \
                         patch("router.desktop.send_command") as send, \
                         patch("router.desktop.wake_pc") as wake, \
                         patch("router.home_assistant.call_service") as ha:
                        command = resolve(phrase + ".", voice_targets()) if mode == "voice" else phrase
                        result = route(command)
                        self.assertEqual(result.action, action)
                        send.assert_called_once_with(action, {})
                        wake.assert_not_called()
                        ha.assert_not_called()

    def test_power_http_contract(self):
        for action in self.phrases:
            with self.subTest(action=action), \
                 patch.dict("os.environ", {"DESKTOP_URL": "http://desktop:5000/"}), \
                 patch.object(desktop, "uuid4", return_value="test-id"), \
                 patch.object(desktop.requests, "post") as post:
                post.return_value.json.return_value = {"requestId": "test-id", "success": True}
                route(f"{action} pc")
                post.assert_called_once_with(
                    "http://desktop:5000/commands",
                    json={"requestId": "test-id", "command": action, "parameters": {}},
                    timeout=5,
                )

    def test_failure_is_not_retried_or_reported_as_accepted(self):
        for action in self.phrases:
            output = io.StringIO()
            with self.subTest(action=action), patch("sys.argv", ["main.py", "--text"]), \
                 patch("builtins.input", side_effect=[f"{action} pc", "exit"]), \
                 patch("router.desktop.send_command", side_effect=requests.Timeout) as send, \
                 patch("router.desktop.wake_pc") as wake, redirect_stdout(output):
                main.main()
            send.assert_called_once_with(action, {})
            wake.assert_not_called()
            self.assertNotIn(f"{action.capitalize()} accepted", output.getvalue())

    def test_failed_desktop_response_is_not_reported_as_accepted(self):
        for action in self.phrases:
            output = io.StringIO()
            with self.subTest(action=action), patch("sys.argv", ["main.py", "--text"]), \
                 patch("builtins.input", side_effect=[f"{action} pc", "exit"]), \
                 patch.dict("os.environ", {"DESKTOP_URL": "http://desktop:5000"}), \
                 patch.object(desktop, "uuid4", return_value="test-id"), \
                 patch.object(desktop.requests, "post") as post, \
                 patch("router.desktop.wake_pc") as wake, redirect_stdout(output):
                post.return_value.json.return_value = {
                    "requestId": "test-id", "success": False, "message": "Power command rejected."
                }
                main.main()
            post.assert_called_once()
            wake.assert_not_called()
            self.assertIn("Power command rejected.", output.getvalue())
            self.assertNotIn(f"{action.capitalize()} accepted", output.getvalue())

    def test_acknowledgment_is_reported(self):
        for action in self.phrases:
            output = io.StringIO()
            with self.subTest(action=action), patch("sys.argv", ["main.py", "--text"]), \
                 patch("builtins.input", side_effect=[f"{action} pc", "exit"]), \
                 patch("router.desktop.send_command", return_value={"success": True}), \
                 redirect_stdout(output):
                main.main()
            self.assertIn(f"{action.capitalize()} accepted by the desktop client.", output.getvalue())

    def test_unsupported_targets_do_not_execute_power_commands(self):
        for phrase in ["sleep bathroom", "restart notepad", "sleep", "restart"]:
            with self.subTest(phrase=phrase), patch("router.desktop.send_command") as send, \
                 patch("router.desktop.wake_pc") as wake:
                with self.assertRaises(ValueError):
                    route(phrase)
                with self.assertRaises(ValueError):
                    resolve(phrase, voice_targets())
                send.assert_not_called()
                wake.assert_not_called()
