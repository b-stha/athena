"""OVOS intents for Athena's existing home and desktop backends."""

from dataclasses import asdict

from requests.exceptions import RequestException
from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.intents import IntentBuilder
from ovos_workshop.skills.fallback import FallbackSkill

from router import COMMAND_TEMPLATES, TARGET_VOCABULARY, UnknownCommand, execute_action, parse_action


CONTEXT_REQUEST = "athena.context.request"
ACTION_RESULT = "athena.action.result"


def utterance_from_message(message):
    """Use the original utterance, rather than trusting intent slot values."""
    utterance = message.data.get("utterance")
    if isinstance(utterance, str):
        return utterance
    utterances = message.data.get("utterances", [])
    if isinstance(utterances, list) and utterances and isinstance(utterances[0], str):
        return utterances[0]
    return ""


class AthenaSkill(FallbackSkill):
    """Deterministic intents first, with one explicit contextual extension point."""

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(internet_before_load=False,
                                   network_before_load=False,
                                   requires_internet=False,
                                   requires_network=True,
                                   no_internet_fallback=False,
                                   no_network_fallback=False)

    def initialize(self):
        for kind, vocabulary in TARGET_VOCABULARY.items():
            for target in vocabulary:
                self.register_vocabulary(target, kind + "Target", lang="en-US")
        for template in COMMAND_TEMPLATES:
            for verb in template.verb.split("|"):
                self.register_vocabulary(verb, template.verb_slot, lang="en-US")
            intent = (IntentBuilder(template.name)
                      .require(template.verb_slot)
                      .require(template.target_slot))
            self.register_intent(intent, self.handle_command)
        # Runs after deterministic intent matching. This publishes a future
        # reasoning/MCP boundary; no contextual executor is implemented yet.
        # Core 2.1's fallback_low stage accepts priorities greater than 90.
        self.register_fallback(self.handle_context, 91)

    def can_answer(self, message):
        return bool(utterance_from_message(message).strip())

    def handle_context(self, message):
        utterance = utterance_from_message(message).strip()
        if not utterance:
            return False
        self.bus.emit(message.forward(CONTEXT_REQUEST, {"utterance": utterance,
                                                       "reason": "no_deterministic_match"}))
        self.log.info("Contextual request has no reasoning handler yet.")
        self.speak("I don't have a command for that yet.")
        return True

    def handle_command(self, message):
        # Revalidate the full sentence at the execution boundary. This also
        # prevents forged/stale slots from selecting an unintended action.
        try:
            action = parse_action(utterance_from_message(message))
        except UnknownCommand:
            return self.handle_context(message)
        try:
            execute_action(action)
        except (RequestException, ValueError) as error:
            self.log.error("Backend command failed: %s", error)
            self.bus.emit(message.forward(ACTION_RESULT, {"success": False,
                                                        "action": asdict(action),
                                                        "error": str(error)}))
            self.speak("The command failed. Check the backend connection and configuration.")
            return True
        self.bus.emit(message.forward(ACTION_RESULT, {"success": True, "action": asdict(action)}))
        if action.backend == "desktop" and action.action == "wake":
            self.speak("Wake packet sent. PC startup is not confirmed.")
        elif action.backend == "desktop" and action.action in {"shutdown", "restart", "sleep"}:
            self.speak(f"{action.action.capitalize()} accepted by the desktop client.")
        else:
            self.speak("Done.")
        return True


def create_skill():
    return AthenaSkill()
