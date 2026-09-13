from dataclasses import dataclass

from integrations import home_assistant


@dataclass(frozen=True)
class Action:
    backend: str
    action: str
    target: str
    target_type: str = "entity_id"


ACTIONS = {"turn on": "turn_on", "turn off": "turn_off"}

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
    words = normalized.split(" ", 2)
    verb = " ".join(words[:2])
    if len(words) != 3 or verb not in ACTIONS:
        raise ValueError("Unknown command. Try: turn on desk lights")

    name = ALIASES.get(words[2], words[2])
    if name not in TARGETS:
        raise ValueError("Unknown light or room. Try: desk lights, table glow, under glow, bedroom, or bathroom")
    target_type, target = TARGETS[name]
    action = Action("home_assistant", ACTIONS[verb], target, target_type)

    if action.backend == "home_assistant":
        if action.target_type == "area_id":
            home_assistant.call_service("light", action.action, area_id=action.target)
        else:
            domain = action.target.split(".", 1)[0]
            home_assistant.call_service(domain, action.action, action.target)
    else:
        raise ValueError(f"Unsupported backend: {action.backend}")

    return action
