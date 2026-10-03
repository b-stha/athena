"""Run Athena's Adapt skill without unrelated training or connectivity probes."""

import ovos_core.__main__ as core
from ovos_core.skill_manager import SkillManager

from athena_skill import AthenaSkill


class AthenaSkillManager(SkillManager):
    """Use the native skill lifecycle with Athena as the only managed skill."""

    def _load_new_skills(self, network=None, internet=None, gui=None):
        # Adapt registers Athena's intents during loading and needs no separate
        # training acknowledgement. The inherited loader reports success.
        if "athena-skill" not in self.plugin_skills and "athena-skill" not in self.blacklist:
            self._load_plugin_skill("athena-skill", AthenaSkill)

    def _sync_skill_loading_state(self):
        # There are no skills waiting for connectivity. Complete those loading
        # phases without claiming a working connection to either backend.
        self._network_loaded.set()
        self._internet_loaded.set()


def main():
    # Core 2.1.1 has no manager factory argument. Replace its constructor only
    # in this dedicated child process, retaining its services and shutdown.
    native_manager = core.SkillManager
    core.SkillManager = AthenaSkillManager
    try:
        core.main(enable_installer=False)
    finally:
        core.SkillManager = native_manager


if __name__ == "__main__":
    main()
