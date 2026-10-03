"""Keep recordings without confirmed command speech out of transcription."""

from dataclasses import dataclass, field, fields

from ovos_dinkum_listener.service import OVOSDinkumVoiceService
from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop, ListeningState
from ovos_utils.log import LOG


@dataclass
class AthenaVoiceLoop(DinkumVoiceLoop):
    """Reuse OVOS's speech decision and its normal recording cleanup."""

    _speech_confirmed: bool = field(default=False, init=False, repr=False)

    def reset_speech_timer(self):
        self._speech_confirmed = False
        super().reset_speech_timer()

    def start(self):
        self._speech_confirmed = False
        super().start()

    def reset_state(self):
        self._speech_confirmed = False
        super().reset_state()

    def _before_cmd(self, chunk):
        super()._before_cmd(chunk)
        if self.state == ListeningState.IN_COMMAND:
            self._speech_confirmed = True

    def _wait_cmd(self, chunk):
        super()._wait_cmd(chunk)
        if self.state == ListeningState.IN_COMMAND:
            self._speech_confirmed = True

    def _vad_remove_silence(self):
        if self._speech_confirmed:
            super()._vad_remove_silence()

    def _get_tx(self, stt_context):
        if self._speech_confirmed:
            return super()._get_tx(stt_context)

        LOG.info("Discarding recording: no command speech detected.")
        # BufferedWhisperSTT.shutdown() joins and clears the worker without
        # finalize(), which would send this rejected recording to Whisper.
        self.stt.shutdown()
        return [], stt_context

    def _after_cmd(self, chunk):
        try:
            # Preserve OVOS's recording-end callback, empty-result event,
            # audio/timer cleanup, and wake/VAD reset for both outcomes.
            super()._after_cmd(chunk)
        finally:
            self._speech_confirmed = False


class AthenaVoiceService(OVOSDinkumVoiceService):
    """Create the guarded loop through the native initialization/reload hook."""

    def _init_voice_loop(self, listener_config):
        loop = super()._init_voice_loop(listener_config)
        # The native factory supplies all configuration and bound callbacks.
        # Its newly constructed loop has not started or opened the microphone.
        options = {item.name: getattr(loop, item.name)
                   for item in fields(loop) if item.init}
        return AthenaVoiceLoop(**options)
