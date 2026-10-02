"""Closed command grammar and backend dispatch shared by OVOS and text mode."""

import re
from dataclasses import dataclass

from integrations import desktop, home_assistant


@dataclass(frozen=True)
class Action:
    backend: str
    action: str
    target: str
    target_type: str = "entity_id"


class UnknownCommand(ValueError):
    """The utterance has no complete, unambiguous deterministic match."""


ACTIONS = {"turn on": "turn_on", "turn off": "turn_off"}
TARGETS = {
    "nanoleafs": ("entity_id", "light.nanoleafs"),
    "table glow": ("entity_id", "light.table_glow"),
    "under glow": ("entity_id", "light.under_glow"),
    "bathroom": ("entity_id", "light.bathroom"),
    "bedroom": ("area_id", "bedroom"),
}
ALIASES = {
    "underglow": "under glow",
    "bathroom lights": "bathroom",
    "bedroom lights": "bedroom",
}
DESKTOP_TARGETS = {"pc": "pc", "computer": "pc", "desktop": "pc"}
APPLICATIONS = {"notepad": "notepad"}
LIGHT_TARGETS = {name: name for name in TARGETS} | ALIASES
TARGET_VOCABULARY = {"Desktop": DESKTOP_TARGETS, "Application": APPLICATIONS, "Light": LIGHT_TARGETS}
PUNCTUATION = str.maketrans({mark: " " for mark in ",.!?;:"})


def normalize_command(command):
    if not isinstance(command, str):
        raise UnknownCommand("A command must be text.")
    return " ".join(command.lower().translate(PUNCTUATION).split())


def _alternatives(values):
    return "(?:" + "|".join(re.escape(value) for value in sorted(values, key=len, reverse=True)) + ")"


@dataclass(frozen=True)
class CommandTemplate:
    """One action pattern with reusable target vocabulary, not full phrases."""

    name: str
    backend: str
    action: str
    target_kind: str
    verb: str
    suffix: str = ""

    @property
    def verb_slot(self):
        return self.name + "Verb"

    @property
    def target_slot(self):
        return self.target_kind + "Target"

    @property
    def pattern(self):
        target = rf"(?P<{self.target_slot}>{_alternatives(TARGET_VOCABULARY[self.target_kind])})"
        if self.target_kind == "Desktop":
            target = r"(?:(?:my|the) )?" + target
        verb = rf"(?P<{self.verb_slot}>{self.verb})"
        # Full sentence validation supplements Adapt's keyword matching:
        # negation, questions, conditionals and extra commands cannot execute.
        return rf"^(?:please )?{verb} {target}{self.suffix}(?: please)?$"

    def match(self, command):
        match = re.fullmatch(self.pattern, command)
        if match is None:
            return None
        spoken_target = match.group(self.target_slot)
        if self.target_kind == "Desktop":
            return Action("desktop", self.action, DESKTOP_TARGETS[spoken_target], "device")
        if self.target_kind == "Application":
            return Action("desktop", self.action, APPLICATIONS[spoken_target], "application")
        target_type, target = TARGETS[LIGHT_TARGETS[spoken_target]]
        return Action("home_assistant", self.action, target, target_type)


COMMAND_TEMPLATES = (
    CommandTemplate("OpenApplication", "desktop", "launch_app", "Application", r"open|launch|start"),
    CommandTemplate("WakeDesktop", "desktop", "wake", "Desktop", r"turn on|wake up|wake|power on|switch on"),
    CommandTemplate("ShutdownDesktop", "desktop", "shutdown", "Desktop", r"shutdown|shut down|turn off|power off|switch off"),
    CommandTemplate("RestartDesktop", "desktop", "restart", "Desktop", r"restart|reboot"),
    CommandTemplate("SleepDesktop", "desktop", "sleep", "Desktop", r"sleep|suspend"),
    CommandTemplate("PutDesktopToSleep", "desktop", "sleep", "Desktop", r"put|send", " to sleep"),
    CommandTemplate("TurnOnLight", "home_assistant", "turn_on", "Light", r"turn on|switch on"),
    CommandTemplate("TurnOffLight", "home_assistant", "turn_off", "Light", r"turn off|switch off"),
)


def parse_action(command):
    """Recognize a complete command without performing any I/O."""
    normalized = normalize_command(command)
    matches = {action for template in COMMAND_TEMPLATES if (action := template.match(normalized)) is not None}
    if len(matches) != 1:
        raise UnknownCommand("Unknown or ambiguous command. Try: turn on nanoleafs or open notepad")
    return matches.pop()


def validate_action(action):
    """Validate a structured action before any backend can execute it."""
    if not isinstance(action, Action):
        raise ValueError("Expected an Athena Action.")
    if action.backend == "desktop":
        if action.action == "launch_app" and action.target_type == "application" and action.target in APPLICATIONS.values():
            return action
        if action.action in {"wake", "shutdown", "restart", "sleep"} and action.target == "pc" and action.target_type == "device":
            return action
    elif action.backend == "home_assistant":
        if action.action in ACTIONS.values() and (action.target_type, action.target) in TARGETS.values():
            return action
    raise ValueError("Unsupported backend action or target.")


def execute_action(action):
    """Execute only validated actions; backend failures never become fallback."""
    validate_action(action)
    if action.backend == "home_assistant":
        if action.target_type == "area_id":
            home_assistant.call_service("light", action.action, area_id=action.target)
        else:
            domain = action.target.split(".", 1)[0]
            home_assistant.call_service(domain, action.action, action.target)
    else:
        desktop.execute(action.action, action.target)
    return action


def route(command):
    """Preserve the small text-mode entry point for backend diagnostics."""
    return execute_action(parse_action(command))


def voice_targets():
    """Legacy resolver vocabulary; OVOS uses the strict templates directly."""
    targets = {verb: dict(LIGHT_TARGETS) for verb in ACTIONS}
    devices = {name: "pc" for name in DESKTOP_TARGETS}
    devices.update({"my " + name: "my pc" for name in DESKTOP_TARGETS})
    for verb in ("turn on", "turn off", "shutdown", "shut down", "restart", "sleep"):
        targets.setdefault(verb, {}).update(devices)
    targets["open"] = dict(APPLICATIONS)
    return targets


def resolve_light_action(normalized):
    """Compatibility helper for callers that explicitly expect a light."""
    action = parse_action(normalized)
    if action.backend != "home_assistant":
        raise UnknownCommand("Unknown light or room.")
    return action
