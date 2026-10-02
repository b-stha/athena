"""Microphone format shared by manual recording, wake detection and Whisper."""

import sys
from array import array

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
CAPTURE_CHANNELS = 2
SOURCE_CHANNEL = 2
INPUT_DEVICE = "reSpeaker Flex XVF3800"
MAX_SECONDS = 30

FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH


def source_audio(data):
    """Copy the processed microphone channel into little-endian mono PCM."""
    samples = array("h")
    samples.frombytes(bytes(data))
    mono = samples[SOURCE_CHANNEL - 1::CAPTURE_CHANNELS]
    if sys.byteorder != "little":
        mono.byteswap()
    return mono.tobytes()
