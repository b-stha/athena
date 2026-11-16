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
        return self.build_pattern()

    def build_pattern(self, *, fuzzy=False, applications=None):
        targets = (APPLICATIONS if applications is None else applications) if self.target_kind == "Application" else TARGET_VOCABULARY[self.target_kind]
        vocabulary = r'[a-z]+(?: [a-z]+){0,2}?' if fuzzy else _alternatives(targets)
        target = rf"(?P<{self.target_slot}>{vocabulary})"
        prefix = r"(?:please )?"
        if self.target_kind == "Desktop":
            target = r"(?:(?:my|the) )?" + target
        elif self.target_kind == "Application":
            target = r"(?:the )?" + target
        elif self.target_kind == "Light":
            target = r"(?:the )?" + target
            if self.verb:
                prefix += r"(?:(?:can|could|would) you (?:please )?)?"
        verb = rf"(?P<{self.verb_slot}>{self.verb}) " if self.verb else ""
        # Full sentence validation supplements Adapt's keyword matching:
        # negation, conditionals and extra commands cannot execute.
        return rf"^{prefix}{verb}{target}{self.suffix}(?: please)?$"

    def match(self, command, *, target_resolver=None, applications=None):
        match = re.fullmatch(self.build_pattern(fuzzy=target_resolver is not None,
                                              applications=applications), command)
        if match is None:
            return None
        spoken_target = match.group(self.target_slot)
        if self.target_kind == "Application":
            vocabulary = APPLICATIONS if applications is None else applications
            if target_resolver is not None:
                # Fuzzy speech slots contain words only. Symbol/number-qualified
                # names need an exact or spoken alias; an unspoken "++" must
                # not become a competing correction for ordinary Notepad.
                vocabulary = {name: target for name, target in vocabulary.items()
                              if re.fullmatch(r'[a-z]+(?: [a-z]+){0,2}', name)}
            target = (target_resolver(spoken_target, vocabulary) if target_resolver is not None
                      else vocabulary[spoken_target])
            if isinstance(target, tuple):
                if len(target) != 1:
                    from resolver import AmbiguousTarget
                    raise AmbiguousTarget('Multiple Windows applications share that name.')
                target = target[0]
            return Action("desktop", self.action, target, "application")
        if target_resolver is not None:
            spoken_target = target_resolver(spoken_target, TARGET_VOCABULARY[self.target_kind])
        if self.target_kind == "Desktop":
            return Action("desktop", self.action, DESKTOP_TARGETS[spoken_target], "device")
        target_type, target = TARGETS[LIGHT_TARGETS[spoken_target]]
        return Action("home_assistant", self.action, target, target_type)


COMMAND_TEMPLATES = (
    CommandTemplate("OpenApplication", "desktop", "launch_app", "Application", r"open|launch|start"),
    CommandTemplate("CloseApplication", "desktop", "close_app", "Application", r"close|exit|quit"),
    CommandTemplate("WakeDesktop", "desktop", "wake", "Desktop", r"turn on|wake up|wake|power on|switch on"),
    CommandTemplate("ShutdownDesktop", "desktop", "shutdown", "Desktop", r"shutdown|shut down|turn off|power off|switch off"),
    CommandTemplate("RestartDesktop", "desktop", "restart", "Desktop", r"restart|reboot"),
    CommandTemplate("SleepDesktop", "desktop", "sleep", "Desktop", r"sleep|suspend"),
    CommandTemplate("PutDesktopToSleep", "desktop", "sleep", "Desktop", r"put|send", " to sleep"),
    CommandTemplate("TurnOnLight", "home_assistant", "turn_on", "Light", r"turn on|switch on"),
    CommandTemplate("TurnOffLight", "home_assistant", "turn_off", "Light", r"turn off|switch off"),
    CommandTemplate("TurnLightOn", "home_assistant", "turn_on", "Light", r"turn|switch", " on"),
    CommandTemplate("TurnLightOff", "home_assistant", "turn_off", "Light", r"turn|switch", " off"),
    CommandTemplate("LightOn", "home_assistant", "turn_on", "Light", "", " on"),
    CommandTemplate("LightOff", "home_assistant", "turn_off", "Light", "", " off"),
)


def is_explicit_light_request(command):
    """Recognize a complete light request with an action verb, without I/O.

    Used only to distinguish terminal request punctuation from a state question.
    Bare target/state forms such as 'bathroom lights off?' remain ambiguous.
    """
    normalized = normalize_command(command)
    return any(template.target_kind == "Light" and template.verb
               and template.match(normalized) is not None for template in COMMAND_TEMPLATES)


def parse_action(command, *, applications=None):
    """Recognize a complete command without performing any I/O."""
    normalized = normalize_command(command)
    if '?' in command and not (command.count('?') == 1 and command.rstrip().endswith('?')
                               and is_explicit_light_request(command)):
        raise UnknownCommand("A question must be resolved before choosing an action.")
    matches = {action for template in COMMAND_TEMPLATES
               if (action := template.match(normalized, applications=applications)) is not None}
    if len(matches) != 1:
        raise UnknownCommand("Unknown or ambiguous command. Try: turn on nanoleafs or open notepad")
    return matches.pop()


def application_scope_view(command, *, applications=None, boundaries=()):
    """Mask only complete registered app slots, retaining every source offset.

    An app can be called "No Man's Sky" or contain "and". Scope words outside
    its exact target slot still guard the entire request. No arbitrary text is
    removed and trailing questions remain questions.
    """
    vocabulary = APPLICATIONS if applications is None else applications
    names = set(vocabulary)
    if 'notepad' in names:
        names.add('no pad')
    if not names or len(boundaries) > 8:
        return command
    targets = _alternatives(names).replace(r'\ ', r'\s+')
    pattern = re.compile(rf'(?:please\s+)?(?:open|launch|start|close|exit|quit)\s+(?:the\s+)?'
                         rf'(?P<target>{targets})(?:\s+please)?[.!]?', re.IGNORECASE)
    starts = [0, *(boundary.end() for boundary in boundaries)]
    ends = [*(boundary.start() for boundary in boundaries), len(command)]
    view = list(command)
    for start in starts:
        # Try the longest complete registered name before a shorter clause.
        for end in reversed(ends):
            if end <= start:
                continue
            left, right = start, end
            while left < right and command[left].isspace():
                left += 1
            while right > left and command[right - 1].isspace():
                right -= 1
            match = pattern.fullmatch(command, left, right)
            if match:
                a, b = match.span('target')
                view[a:b] = 'x' * (b - a)
                break
    return ''.join(view)


def parse_fuzzy_action(command, *, applications=None):
    """Reuse exact command templates with bounded target-only corrections."""
    from resolver import AmbiguousTarget, resolve_target
    normalized = normalize_command(command)
    if '?' in command:
        raise UnknownCommand('Resolve question scope before correcting a target.')
    matches, ambiguous = set(), False
    for template in COMMAND_TEMPLATES:
        if template.target_kind not in {'Light', 'Application'} or not template.verb:
            continue
        try:
            action = template.match(normalized, target_resolver=resolve_target,
                                    applications=applications)
        except AmbiguousTarget:
            ambiguous = True
            continue
        except ValueError:
            continue
        if action is not None:
            matches.add(action)
    if len(matches) == 1 and not ambiguous:
        return validate_action(matches.pop(), applications=applications)
    if ambiguous or len(matches) > 1:
        raise AmbiguousTarget('Please repeat the full target name.')
    raise UnknownCommand('No unambiguous registered fuzzy target.')


def validate_action(action, *, applications=None):
    """Validate a structured action before any backend can execute it."""
    if not isinstance(action, Action):
        raise ValueError("Expected an Athena Action.")
    if action.backend == "desktop":
        vocabulary = APPLICATIONS if applications is None else applications
        app_ids = {app_id for value in vocabulary.values()
                   for app_id in (value if isinstance(value, tuple) else (value,))}
        if action.action in {"launch_app", "close_app"} and action.target_type == "application" and action.target in app_ids:
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
    if action.backend == "desktop" and action.action == "close_app":
        raise ValueError("Graceful closing requires normal voice/text Windows MCP routing.")
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
