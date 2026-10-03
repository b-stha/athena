"""Check Athena-only loading while retaining OVOS's real plugin lifecycle."""

import tempfile
import unittest
from unittest.mock import Mock, patch

from ovos_core.skill_manager import SkillManager
from ovos_utils.fakebus import FakeBus
from ovos_workshop.skill_launcher import PluginSkillLoader

from athena_ovos import skills
from athena_skill import AthenaSkill


class AthenaSkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="athena-manager-test-")
        self.addCleanup(self.temporary.cleanup)
        isolated = {
            "HOME": self.temporary.name,
            "XDG_CONFIG_HOME": self.temporary.name + "/config",
            "XDG_DATA_HOME": self.temporary.name + "/data",
            "XDG_CACHE_HOME": self.temporary.name + "/cache",
        }
        self.environment = patch.dict("os.environ", isolated)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.extra_skill = Mock(name="unrelated-installed-skill")
        discovery = patch("ovos_core.skill_manager.find_skill_plugins",
                          return_value={"athena-skill": AthenaSkill,
                                        "unrelated-skill": self.extra_skill})
        discovery.start()
        self.addCleanup(discovery.stop)
        http = patch("requests.sessions.Session.request",
                     side_effect=AssertionError("Startup must not request HTTP connectivity."))
        self.http = http.start()
        self.addCleanup(http.stop)
        probe = patch("ovos_core.skill_manager.is_connected_http",
                      side_effect=AssertionError("Startup must not probe internet connectivity."))
        self.probe = probe.start()
        self.addCleanup(probe.stop)
        self.bus = FakeBus()
        self.addCleanup(self.bus.close)
        self.bus.wait_for_response = Mock(wraps=self.bus.wait_for_response)
        self.manager = skills.AthenaSkillManager(self.bus, enable_file_watcher=False)
        self.addCleanup(self.manager.shutdown)

    def assert_no_startup_probes_or_training(self):
        self.http.assert_not_called()
        self.probe.assert_not_called()
        requests = [call.args[0].msg_type for call in self.bus.wait_for_response.call_args_list]
        self.assertNotIn("mycroft.skills.train", requests)
        self.assertNotIn("ovos.PHAL.internet_check", requests)

    def test_only_athena_loads_once_even_when_another_plugin_is_installed(self):
        self.manager._load_new_skills(network=False, internet=False, gui=False)
        self.assertEqual(set(self.manager.plugin_skills), {"athena-skill"})
        loader = self.manager.plugin_skills["athena-skill"]
        self.assertIsInstance(loader, PluginSkillLoader)
        self.assertIsInstance(loader.instance, AthenaSkill)
        self.assertTrue(loader.instance.is_fully_initialized)
        self.manager._load_new_skills(network=True, internet=True, gui=True)
        self.assertIs(self.manager.plugin_skills["athena-skill"], loader)
        self.extra_skill.assert_not_called()
        self.assert_no_startup_probes_or_training()

    def test_completed_loading_phases_do_not_fabricate_connected_state(self):
        for network, internet in ((False, False), (True, False), (True, True)):
            with self.subTest(network=network, internet=internet):
                for event, connected in ((self.manager._network_event, network),
                                         (self.manager._connected_event, internet)):
                    event.set() if connected else event.clear()
                self.manager._sync_skill_loading_state()
                self.assertTrue(self.manager._network_loaded.is_set())
                self.assertTrue(self.manager._internet_loaded.is_set())
                self.assertEqual(self.manager._network_event.is_set(), network)
                self.assertEqual(self.manager._connected_event.is_set(), internet)
        self.assert_no_startup_probes_or_training()

    def test_native_reload_unload_and_shutdown_remain_available(self):
        self.manager._load_new_skills()
        loader = self.manager.plugin_skills["athena-skill"]
        first = loader.instance
        self.assertTrue(loader.reload())
        self.assertIsInstance(loader.instance, AthenaSkill)
        self.assertIsNot(loader.instance, first)
        self.manager._unload_plugin_skill("athena-skill")
        self.assertEqual(self.manager.plugin_skills, {})
        self.manager._load_new_skills()
        self.assertEqual(set(self.manager.plugin_skills), {"athena-skill"})
        self.manager.shutdown()
        self.assertEqual(self.manager.plugin_skills, {})
        self.assertTrue(self.manager._stop_event.is_set())
        self.assertIs(skills.AthenaSkillManager._unload_plugin_skill, SkillManager._unload_plugin_skill)
        self.assertIs(skills.AthenaSkillManager.shutdown, SkillManager.shutdown)
        self.assert_no_startup_probes_or_training()

    def test_blacklisted_athena_is_not_loaded(self):
        with patch("ovos_core.skill_manager.Configuration",
                   return_value={"skills": {"blacklisted_skills": ["athena-skill"]}}):
            self.manager._load_new_skills()
        self.assertEqual(self.manager.plugin_skills, {})
        self.extra_skill.assert_not_called()
        self.assert_no_startup_probes_or_training()

    def test_entrypoint_restores_native_constructor_when_core_fails(self):
        original = skills.core.SkillManager

        def failing_core(**kwargs):
            self.assertIs(skills.core.SkillManager, skills.AthenaSkillManager)
            self.assertEqual(kwargs, {"enable_installer": False})
            raise RuntimeError("Synthetic startup failure.")

        with patch.object(skills.core, "main", side_effect=failing_core):
            with self.assertRaisesRegex(RuntimeError, "Synthetic startup failure"):
                skills.main()
        self.assertIs(skills.core.SkillManager, original)


if __name__ == "__main__":
    unittest.main()
