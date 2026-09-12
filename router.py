from dataclasses import dataclass

from integrations import home_assistant


@dataclass(frozen=True)
class Action:
    backend: str
    action: str
    target: str


COMMANDS = {
    "turn on desk lights": Action("home_assistant", "turn_on", "light.nanoleafs"),
}


def route(command):
    normalized = " ".join(command.lower().split())
    action = COMMANDS.get(normalized)
    if action is None:
        raise ValueError("Unknown command. Try: turn on desk lights")

    if action.backend == "home_assistant":
        domain = action.target.split(".", 1)[0]
        home_assistant.call_service(domain, action.action, action.target)
    else:
        raise ValueError(f"Unsupported backend: {action.backend}")

    return action
