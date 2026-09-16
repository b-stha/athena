import unittest
from unittest.mock import patch

import requests

from integrations import desktop
from router import route


class DesktopTests(unittest.TestCase):
    def test_routes_to_desktop_only(self):
        with patch("router.desktop.send_command") as send, patch("router.home_assistant.call_service") as ha:
            action = route("  OPEN   Notepad ")
            send.assert_called_once_with("launch_app", {"name": "notepad"})
            ha.assert_not_called()
            self.assertEqual(action.backend, "desktop")

    def test_light_commands_do_not_reach_desktop(self):
        with patch("router.desktop.send_command") as send, patch("router.home_assistant.call_service"):
            route("turn on desk lights")
            send.assert_not_called()

    def test_unknown_app_does_not_send(self):
        with patch("router.desktop.send_command") as send, self.assertRaises(ValueError):
            route("open unknown")
        send.assert_not_called()

    def test_missing_configuration(self):
        with patch.dict("os.environ", {"DESKTOP_URL": ""}), patch.object(desktop.requests, "post") as post:
            with self.assertRaisesRegex(ValueError, "DESKTOP_URL"):
                desktop.send_command("launch_app", {"name": "notepad"})
            post.assert_not_called()

    def test_http_contract_and_response_validation(self):
        with patch.dict("os.environ", {"DESKTOP_URL": "http://desktop:5000/"}), \
             patch.object(desktop, "uuid4", return_value="test-id"), \
             patch.object(desktop.requests, "post") as post:
            response = post.return_value
            response.json.return_value = {"requestId": "test-id", "success": True}
            desktop.send_command("launch_app", {"name": "notepad"})
            post.assert_called_once_with(
                "http://desktop:5000/commands",
                json={"requestId": "test-id", "command": "launch_app", "parameters": {"name": "notepad"}},
                timeout=5,
            )
            for body in [None, {"requestId": "wrong", "success": True},
                         {"requestId": "test-id", "success": False},
                         {"requestId": "test-id", "success": "true"}]:
                response.json.return_value = body
                with self.subTest(body=body), self.assertRaises(ValueError):
                    desktop.send_command("launch_app", {"name": "notepad"})
            response.raise_for_status.side_effect = requests.HTTPError("501")
            with self.assertRaises(requests.HTTPError):
                desktop.send_command("launch_app", {"name": "notepad"})
            post.side_effect = requests.Timeout()
            with self.assertRaises(requests.Timeout):
                desktop.send_command("launch_app", {"name": "notepad"})
