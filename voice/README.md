# Voice input

Run from an interactive Linux terminal on the machine with the microphone attached.
An SSH terminal controls keys on the remote machine; it does not forward your laptop microphone.

```bash
sudo apt-get install libportaudio2
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Press and release Space to start speaking, then press Space again to stop.
Recording stops automatically after 30 seconds. Athena prints the Whisper transcript
and resolves its target before passing the command to the router. Say `turn on nanoleafs` to operate
`light.nanoleafs`. Ctrl-C quits. `.venv/bin/python main.py --text` retains typed input.

Whisper must already be running as a Wyoming service. The default endpoint is
`tcp://127.0.0.1:10300`, matching the port in `/home/MK/docker/whisper/compose.yaml`.
Set `WHISPER_URI` in `.env` if it runs elsewhere. The client does not start Docker
or load a model. Requests use English and have a 120-second overall timeout.

`input.py` captures the first two channels of the reSpeaker Flex XVF3800 at 16 kHz
and selects physical channel 2 as 16-bit mono PCM for Whisper.
`stt.py` peak-normalizes each recording to -1 dBFS in memory (equivalent to
`sox gain -n -1`), sends a Wyoming transcription request and audio events, then returns the
transcript. Audio stays in memory and is discarded after the interaction.
`main.py` calls `transcribe()` first, displays the original transcript, then calls
`resolve()` in the project-root `resolver.py` before routing. The resolver accepts
text and the router's target vocabulary; it does not record, transcribe, or print.
RapidFuzz's Levenshtein distance functions provide the matching scores.
Action phrases must match exactly. Target comparison ignores spaces and uses edit distance:
at most five edits and 65% of the longer name, with no approximate matching for input
names shorter than five characters. Competing matches need a similarity gap of at least
0.15; otherwise the request is rejected for clarification. These are initial heuristic
thresholds, not confidence probabilities. A changed command is shown as `Matched:`.

For example, `open no pad`, `open node pad`, and `open note pad` resolve to
`open notepad` without per-app mishearing aliases. Text mode bypasses speech correction
and requires commands accepted by the deterministic router. No LLM is used.

Run automated checks with `.venv/bin/python -m unittest discover -s tests`.
To verify hardware, start the program, record the supported command, and check
the displayed transcript and light response. Also try an unsupported command and
Ctrl-C during recording. These live steps require an available microphone and services.

Protocol reference: https://github.com/rhasspy/wyoming
Capture reference: https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html

Scoring reference: https://rapidfuzz.github.io/RapidFuzz/Usage/distance/Levenshtein.html
