"""Spacebar-controlled recording from the local microphone (Linux terminal)."""

import os
import select
import sys
import termios
import time
import tty

import sounddevice as sd

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
MAX_SECONDS = 30


def record():
    """Return signed 16-bit mono PCM; restore the terminal on every exit."""
    if not sys.stdin.isatty():
        raise RuntimeError("Voice input requires an interactive terminal.")

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    chunks = []
    errors = []

    def capture(data, frames, timing, status):
        if status:
            errors.append(str(status))
        chunks.append(bytes(data))

    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        print("Press Space to speak (Ctrl-C to quit).", flush=True)
        while True:
            key = os.read(fd, 1)
            if not key:
                raise EOFError
            if key == b" ":
                break

        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                               dtype="int16", callback=capture):
            print("Recording — press Space to stop (30-second limit).", flush=True)
            deadline = time.monotonic() + MAX_SECONDS
            while time.monotonic() < deadline:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    key = os.read(fd, 1)
                    if not key:
                        raise EOFError
                    if key == b" ":
                        break
    except sd.PortAudioError as error:
        raise RuntimeError(f"Microphone unavailable: {error}") from error
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    if errors:
        raise RuntimeError("Audio capture failed; please try recording again.")
    return b"".join(chunks)
