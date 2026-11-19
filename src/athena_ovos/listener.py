"""Keep recordings without confirmed command speech out of transcription."""

from dataclasses import dataclass, field, fields
from collections import deque

from ovos_dinkum_listener.service import OVOSDinkumVoiceService
from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop, ListeningState
from ovos_utils.log import LOG


def _command_speech(audio, vad):
    """Trim outside speech while preserving every segment and intervening pause.

    WebRTC's upstream extract_speech returns at the first completed segment.
    Use its existing classifier and padding, but continue to the last segment.
    None leaves the original recording untouched.
    """
    frame_bytes = int(vad.sample_rate * vad.frame_duration_ms / 1000) * 2
    if not audio or len(audio) % 2:
        return None
    padding = deque(maxlen=vad.num_padding_frames)
    first, last, triggered = None, None, False
    vad.reset()
    try:
        for offset in range(0, len(audio), frame_bytes):
            frame = audio[offset:offset + frame_bytes]
            # Microphone chunks are 20 ms; final VAD frames may be 30 ms.
            # Pad only the classifier input and retain the original PCM tail.
            speech = not vad.is_silence(frame.ljust(frame_bytes, b'\0'))
            padding.append((offset, speech))
            if not triggered:
                if sum(item[1] for item in padding) > vad.thresh * padding.maxlen:
                    triggered = True
                    if first is None:
                        first = padding[0][0]
                    last = min(offset + frame_bytes, len(audio))
                    padding.clear()
            else:
                last = min(offset + frame_bytes, len(audio))
                if sum(not item[1] for item in padding) > vad.thresh * padding.maxlen:
                    triggered = False
                    padding.clear()
    finally:
        vad.reset()
    if first is not None:
        return audio[first:last]
    return None


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
        if not self._speech_confirmed:
            return
        self.stt.finish_buffering()
        # Match the native minimum-length guard, retaining rewound audio when
        # classification is uncertain. Replace only after the worker has drained.
        if len(self.stt_audio_bytes) <= self.vad.sample_rate * 2:
            return
        speech = _command_speech(self.stt_audio_bytes, self.vad)
        if speech and len(speech) >= self.vad.sample_rate * 2:
            self.stt.stream.buffer.clear()
            self.stt.stream.update(speech)

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
