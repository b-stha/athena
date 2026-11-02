"""Forward completed OVOS transcripts to Athena's Pi-hosted agent."""

from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.skills.fallback import FallbackSkill

CONTEXT_REQUEST = "athena.context.request"
ACTION_RESULT = "athena.action.result"


def utterance_from_message(message):
    utterance = message.data.get("utterance")
    if isinstance(utterance, str):
        return utterance
    utterances = message.data.get("utterances", [])
    if isinstance(utterances, list) and utterances and isinstance(utterances[0], str):
        return utterances[0]
    return ""


class AthenaSkill(FallbackSkill):
    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(internet_before_load=False, network_before_load=False,
                                   requires_internet=False, requires_network=True,
                                   no_internet_fallback=False, no_network_fallback=False)

    def initialize(self):
        # Construction performs no network calls and opens no thread.
        from athena_agent.worker import AgentWorker
        self._agent_worker = AgentWorker()
        self.register_fallback(self.handle_context, 91)

    def can_answer(self, message):
        return bool(utterance_from_message(message).strip())

    def handle_context(self, message):
        utterance = utterance_from_message(message).strip()
        if not utterance:
            return False
        context = message.context or {}
        session = context.get("session", {})
        session_id = session.get("session_id", "default") if isinstance(session, dict) else "default"
        self.bus.emit(message.forward(CONTEXT_REQUEST, {"utterance": utterance, "reason": "agent_request"}))

        def respond(response):
            self.bus.emit(message.forward(ACTION_RESULT, {"status": "agent_response", "response": response}))
            self.speak(response)

        def fail(response):
            self.log.error("Athena agent request failed.")
            self.bus.emit(message.forward(ACTION_RESULT, {"status": "failed", "error": response}))
            self.speak(response)

        if not self._agent_worker.submit(utterance, respond, fail, session_id=session_id):
            self.speak("I'm still handling other requests. Please try again shortly.")
        return True

    def shutdown(self):
        worker = getattr(self, "_agent_worker", None)
        if worker is not None:
            worker.close()


def create_skill():
    return AthenaSkill()
