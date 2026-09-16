from dataclasses import dataclass

from integrations import desktop, home_assistant, wake_on_lan


@dataclass(frozen=True)
class Action:
    backend: str
    action: str
    target: str
    target_type: str = "entity_id"


ACTIONS = {"turn on": "turn_on", "turn off": "turn_off"}

DESKTOP_COMMANDS = {
    "open notepad": Action("desktop", "launch_app", "notepad", "application"),
}

WAKE_COMMANDS = {
    "turn on pc": Action("wake_on_lan", "wake", "pc", "device"),
    "turn on my pc": Action("wake_on_lan", "wake", "pc", "device"),
}

TARGETS = {
    "desk lights": ("entity_id", "light.nanoleafs"),
    "table glow": ("entity_id", "light.table_glow"),
    "under glow": ("entity_id", "light.under_glow"),
    "bathroom": ("entity_id", "light.bathroom"),
    "bedroom": ("area_id", "bedroom"),
}

ALIASES = {
    "desk light": "desk lights",
    "desks lights": "desk lights",
    "nanoleafs": "desk lights",
    "table glow lights": "table glow",
    "under glow lights": "under glow",
    "underglow": "under glow",
    "bathroom lights": "bathroom",
    "bedroom lights": "bedroom",
}


def route(command):
    normalized = " ".join(command.lower().split())
    action = WAKE_COMMANDS.get(normalized) or DESKTOP_COMMANDS.get(normalized)
    if action is None:
        action = resolve_light_action(normalized)

    if action.backend == "home_assistant":
        if action.target_type == "area_id":
            home_assistant.call_service("light", action.action, area_id=action.target)
        else:
            domain = action.target.split(".", 1)[0]
            home_assistant.call_service(domain, action.action, action.target)
    elif action.backend == "desktop":
        desktop.send_command(action.action, {"name": action.target})
    elif action.backend == "wake_on_lan":
        wake_on_lan.wake_pc()
    else:
        raise ValueError(f"Unsupported backend: {action.backend}")

    return action


def resolve_light_action(normalized):
    words = normalized.split(" ", 2)
    verb = " ".join(words[:2])
    if len(words) != 3 or verb not in ACTIONS:
        raise ValueError("Unknown command. Try: turn on desk lights or open notepad")

    name = ALIASES.get(words[2], words[2])
    if name not in TARGETS:
        raise ValueError("Unknown light or room. Try: desk lights, table glow, under glow, bedroom, or bathroom")
    target_type, target = TARGETS[name]
    return Action("home_assistant", ACTIONS[verb], target, target_type)
