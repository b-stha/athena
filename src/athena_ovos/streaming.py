"""Dispose OVOS's buffered transcription thread without transcribing on exit."""

from ovos_dinkum_listener.plugins import FakeStreamingSTT


class BufferedWhisperSTT(FakeStreamingSTT):
    """Keep OVOS buffering; discard an unfinished command during shutdown."""

    def finish_buffering(self):
        """Drain queued microphone frames before reading or replacing the audio."""
        stream = self.stream
        if stream is None:
            return
        if stream.is_alive():
            self.queue.put(None)
            stream.join(timeout=2)
            if stream.is_alive():
                raise RuntimeError("OVOS transcription buffer did not stop.")

    def transcribe(self, audio=None, lang=None):
        self.finish_buffering()
        return super().transcribe(audio, lang)

    def shutdown(self):
        stream = self.stream
        if stream is None:
            return
        # stream_stop() calls finalize(), which sends the partial recording to
        # Whisper. Ending the worker directly discards it without that request.
        self.finish_buffering()
        stream.buffer.clear()
        self.stream = None
        self.queue = None
        self.transcript_ready.set()
