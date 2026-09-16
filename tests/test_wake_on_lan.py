import socket
import unittest
from unittest.mock import patch

from integrations import wake_on_lan
from router import route


class WakeOnLanTests(unittest.TestCase):
    def test_packet_and_broadcast(self):
        for mac in ["01:23:45:67:89:ab", "01-23-45-67-89-AB", "0123456789ab"]:
            with self.subTest(mac=mac), patch.dict("os.environ", {
                "PC_MAC_ADDRESS": mac, "WOL_BROADCAST_ADDRESS": "192.0.2.255",
            }), patch.object(wake_on_lan.socket, "socket") as factory:
                wake_on_lan.wake_pc()
                sender = factory.return_value.__enter__.return_value
                factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
                sender.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sender.sendto.assert_called_once_with(
                    b"\xff" * 6 + bytes.fromhex("0123456789ab") * 16,
                    ("192.0.2.255", 9),
                )
                factory.return_value.__exit__.assert_called_once()

    def test_invalid_mac_never_sends(self):
        for mac in ["", "not-a-mac", "01:23:45:67:89", "01:23-45:67:89:ab"]:
            with self.subTest(mac=mac), patch.dict("os.environ", {"PC_MAC_ADDRESS": mac}), \
                 patch.object(wake_on_lan.socket, "socket") as factory:
                with self.assertRaises(ValueError):
                    wake_on_lan.wake_pc()
                factory.assert_not_called()

    def test_send_failure_is_reported(self):
        with patch.dict("os.environ", {"PC_MAC_ADDRESS": "0123456789ab"}), \
             patch.object(wake_on_lan.socket, "socket", side_effect=OSError):
            with self.assertRaisesRegex(ValueError, "Could not send"):
                wake_on_lan.wake_pc()

    def test_wake_commands_bypass_http(self):
        with patch("router.wake_on_lan.wake_pc") as wake, \
             patch("router.desktop.send_command") as desktop, \
             patch("router.home_assistant.call_service") as ha:
            for command in ["turn on PC", "  TURN on my PC  "]:
                self.assertEqual(route(command).backend, "wake_on_lan")
            self.assertEqual(wake.call_count, 2)
            for command in ["turn off pc", "turn onn pc"]:
                with self.assertRaises(ValueError):
                    route(command)
            self.assertEqual(wake.call_count, 2)
            desktop.assert_not_called()
            ha.assert_not_called()
