"""Dispose OVOS's buffered transcription thread without transcribing on exit."""

from ovos_dinkum_listener.plugins import FakeStreamingSTT


class BufferedWhisperSTT(FakeStreamingSTT):
    """Keep OVOS buffering; discard an unfinished command during shutdown."""

    def shutdown(self):
        stream = self.stream
        if stream is None:
            return
        # stream_stop() calls finalize(), which sends the partial recording to
        # Whisper. Ending the worker directly discards it without that request.
        self.queue.put(None)
        stream.join(timeout=2)
        if stream.is_alive():
            raise RuntimeError("OVOS transcription buffer did not stop.")
        stream.buffer.clear()
        self.stream = None
        self.queue = None
        self.transcript_ready.set()
