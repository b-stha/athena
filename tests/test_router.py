import unittest
from unittest.mock import patch

from router import route


class RouterTests(unittest.TestCase):
    def test_on_and_off_for_each_target(self):
        targets = {
            "desk lights": "light.nanoleafs",
            "table glow": "light.table_glow",
            "under glow": "light.under_glow",
            "bathroom": "light.bathroom",
        }
        for verb, service in [("turn on", "turn_on"), ("turn off", "turn_off")]:
            for name, entity in targets.items():
                with self.subTest(verb=verb, name=name), patch("router.home_assistant.call_service") as call:
                    route(f"{verb} {name}")
                    call.assert_called_once_with("light", service, entity)
            with patch("router.home_assistant.call_service") as call:
                route(f"{verb} bedroom lights")
                call.assert_called_once_with("light", service, area_id="bedroom")

    def test_device_alias_does_not_change_action(self):
        with patch("router.home_assistant.call_service") as call:
            route("  TURN   OFF desks lights ")
            call.assert_called_once_with("light", "turn_off", "light.nanoleafs")

    def test_unknown_actions_and_individual_bathroom_lights_are_rejected(self):
        with patch("router.home_assistant.call_service") as call:
            for command in ["turn of desk lights", "turn onn desk lights", "toggle bedroom",
                            "turn off bathroom light 1", "turn on", "turn on kitchen"]:
                with self.subTest(command=command), self.assertRaises(ValueError):
                    route(command)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
