"""OVOS adapters for Athena's processed microphone channel and Wyoming Whisper.

OVOS owns wake detection, command recording and silence detection. These plugins
only supply mono microphone chunks and transcribe the recording OVOS provides.
"""

import asyncio
import math
import os
import sys
from array import array
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Optional

import sounddevice as sd
from ovos_plugin_manager.templates.microphone import Microphone
from ovos_plugin_manager.templates.stt import STT
from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
TARGET_PEAK = round(32767 * 10 ** (-1 / 20))


@dataclass
class AthenaMicrophone(Microphone):
    """Feed reSpeaker channel 2 to the OVOS listener as little-endian mono PCM."""

    chunk_size: int = 640
    device: object = "reSpeaker Flex XVF3800"
    capture_channels: int = 2
    source_channel: int = 2
    timeout: float = 2.0
    queue_maxsize: int = 8
    _queue: Queue = field(init=False, repr=False)
    _stream: object = field(default=None, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        if (self.sample_rate, self.sample_width, self.sample_channels) != (16000, 2, 1):
            raise ValueError("Athena's microphone requires 16 kHz, 16-bit mono output.")
        if self.capture_channels < 1 or not 1 <= self.source_channel <= self.capture_channels:
            raise ValueError("source_channel must select an available capture channel.")
        if self.chunk_size <= 0 or self.chunk_size % self.sample_width:
            raise ValueError("chunk_size must contain complete 16-bit samples.")
        if self.timeout <= 0 or self.queue_maxsize < 1:
            raise ValueError("Microphone timeout and queue_maxsize must be positive.")
        self._queue = Queue(maxsize=self.queue_maxsize)

    def _enqueue(self, item):
        try:
            self._queue.put_nowait(item)
        except Full:
            # OVOS can pause its reader while waiting for transcription. Keep
            # current microphone audio rather than replaying old queued audio.
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except Full:
                pass

    def _capture(self, data, frames, time_info, status):
        if not self._running:
            return
        if status:
            self._enqueue(RuntimeError(f"Microphone capture failed: {status}"))
            return
        try:
            expected = frames * self.capture_channels * self.sample_width
            if len(data) != expected:
                raise RuntimeError("Microphone returned an incomplete audio frame.")
            samples = array("h")
            samples.frombytes(bytes(data))
            mono = samples[self.source_channel - 1::self.capture_channels]
            if sys.byteorder != "little":
                mono.byteswap()
            self._enqueue(mono.tobytes())
        except Exception as error:
            self._enqueue(error)

    def start(self):
        if self._running:
            return
        self._queue = Queue(maxsize=self.queue_maxsize)
        self._running = True
        try:
            self._stream = sd.RawInputStream(
                device=self.device, samplerate=self.sample_rate,
                channels=self.capture_channels, dtype="int16",
                blocksize=self.frames_per_chunk, callback=self._capture,
            )
            self._stream.start()
        except BaseException:
            self._running = False
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            raise

    def read_chunk(self) -> Optional[bytes]:
        if not self._running:
            return None
        try:
            chunk = self._queue.get(timeout=self.timeout)
        except Empty as error:
            if not self._running:
                return None
            raise RuntimeError("Microphone stopped producing audio.") from error
        if not self._running:
            return None
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def stop(self):
        self._running = False
        self._enqueue(None)
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()


def normalize_audio(audio):
    """Preserve Athena's peak normalization to -1 dBFS before Whisper."""
    if len(audio) % SAMPLE_WIDTH:
        raise ValueError("Audio must contain complete 16-bit samples.")
    samples = array("h")
    samples.frombytes(audio)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(sample) for sample in samples), default=0)
    if not peak:
        return audio
    gain = TARGET_PEAK / peak
    normalized = array("h", (round(sample * gain) for sample in samples))
    if sys.byteorder != "little":
        normalized.byteswap()
    return normalized.tobytes()


async def _transcribe(audio, uri, language, connect_timeout):
    async with AsyncClient.from_uri(uri, connect_timeout=connect_timeout) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                           channels=1).event())
        for offset in range(0, len(audio), 3200):
            await client.write_event(AudioChunk(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                               channels=1,
                                               audio=audio[offset:offset + 3200]).event())
        await client.write_event(AudioStop().event())
        while True:
            event = await client.read_event()
            if event is None:
                raise RuntimeError("Whisper disconnected before returning a transcript.")
            if event.type == "error":
                raise RuntimeError("Whisper reported a transcription error.")
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text.strip()


class AthenaWhisperSTT(STT):
    """Transcribe OVOS recordings using the existing local Wyoming server."""

    def __init__(self, config=None):
        super().__init__(config)
        self.uri = self.config.get("uri") or os.getenv("WHISPER_URI", "tcp://127.0.0.1:10300")
        self.timeout = float(self.config.get("timeout", 30))
        self.connect_timeout = float(self.config.get("connect_timeout", 5))
        if (isinstance(self.config.get("timeout"), bool)
                or isinstance(self.config.get("connect_timeout"), bool)
                or not all(math.isfinite(value) and value > 0
                           for value in (self.timeout, self.connect_timeout))):
            raise ValueError("Whisper timeouts must be finite and positive.")

    @classproperty
    def runtime_requirements(cls):
        return RuntimeRequirements(
            internet_before_load=False, network_before_load=False,
            requires_internet=False, requires_network=False,
            no_internet_fallback=True, no_network_fallback=True,
        )

    @classproperty
    def available_languages(cls):
        return {"en", "en-US", "en-GB", "en-AU", "en-CA"}

    def execute(self, audio, language=None):
        pcm = audio.get_raw_data(convert_rate=SAMPLE_RATE, convert_width=SAMPLE_WIDTH)
        if not pcm:
            return ""
        lang = (language or self.lang).split("-")[0].lower()
        if lang != "en":
            raise ValueError("Athena's Whisper adapter is configured for English.")

        async def request():
            return await asyncio.wait_for(
                _transcribe(normalize_audio(pcm), self.uri, lang, self.connect_timeout),
                timeout=self.timeout,
            )

        try:
            return asyncio.run(request())
        except TimeoutError as error:
            raise RuntimeError(f"Whisper transcription timed out after {self.timeout:g} seconds.") from error
        except OSError as error:
            raise RuntimeError("Cannot reach Whisper. Check the service and WHISPER_URI.") from error
