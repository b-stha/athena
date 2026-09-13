"""Send recorded PCM to the existing Wyoming Whisper service."""

import asyncio
import os

from dotenv import load_dotenv
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

from voice.input import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH

load_dotenv()


async def _transcribe(audio, uri):
    async with AsyncClient.from_uri(uri, connect_timeout=5) as client:
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                           channels=CHANNELS).event())
        for offset in range(0, len(audio), 3200):
            await client.write_event(AudioChunk(rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                                               channels=CHANNELS,
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


def transcribe(audio):
    if not audio:
        return ""
    uri = os.getenv("WHISPER_URI", "tcp://127.0.0.1:10300")

    async def request():
        return await asyncio.wait_for(_transcribe(audio, uri), timeout=120)

    try:
        return asyncio.run(request())
    except TimeoutError as error:
        raise RuntimeError("Whisper transcription timed out.") from error
    except OSError as error:
        raise RuntimeError("Cannot reach Whisper. Check the service and WHISPER_URI.") from error
