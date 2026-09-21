import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import main
from router import route


class ShutdownTests(unittest.TestCase):
    def test_shutdown_phrases_use_http_without_waking(self):
        for phrase in ["shutdown PC", "shutdown my PC", "shut down PC",
                       "shut down my PC", "turn off PC", "turn off my PC"]:
            with self.subTest(phrase=phrase), patch("router.desktop.send_command") as send, \
                 patch("router.desktop.wake_pc") as wake, \
                 patch("router.home_assistant.call_service") as ha:
                action = route(phrase)
                self.assertEqual(action.action, "shutdown")
                send.assert_called_once_with("shutdown", {})
                wake.assert_not_called()
                ha.assert_not_called()

    def test_failure_is_not_retried_or_reported_as_accepted(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--text"]), \
             patch("builtins.input", side_effect=["shutdown pc", "exit"]), \
             patch("router.desktop.send_command", side_effect=requests.Timeout) as send, \
             patch("router.desktop.wake_pc") as wake, redirect_stdout(output):
            main.main()
        send.assert_called_once_with("shutdown", {})
        wake.assert_not_called()
        self.assertNotIn("Shutdown accepted", output.getvalue())

    def test_acknowledgment_is_reported(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--text"]), \
             patch("builtins.input", side_effect=["shutdown pc", "exit"]), \
             patch("router.desktop.send_command", return_value={"success": True}), \
             redirect_stdout(output):
            main.main()
        self.assertIn("Shutdown accepted by the desktop client.", output.getvalue())
