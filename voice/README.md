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
and passes it to the existing router. Say `turn on nanoleafs` to operate
`light.nanoleafs`. Ctrl-C quits. `.venv/bin/python main.py --text` retains typed input.

Whisper must already be running as a Wyoming service. The default endpoint is
`tcp://127.0.0.1:10300`, matching the port in `/home/MK/docker/whisper/compose.yaml`.
Set `WHISPER_URI` in `.env` if it runs elsewhere. The client does not start Docker
or load a model. Requests use English and have a 120-second overall timeout.

`input.py` records the default PortAudio input device as 16 kHz, 16-bit mono PCM.
`stt.py` sends a Wyoming transcription request and audio events, then returns the
transcript. Audio stays in memory and is discarded after the interaction.
`main.py` shows the transcript and removes trailing sentence punctuation before routing;
it does not perform fuzzy matching or LLM interpretation.

Run automated checks with `.venv/bin/python -m unittest discover -s tests`.
To verify hardware, start the program, record the supported command, and check
the displayed transcript and light response. Also try an unsupported command and
Ctrl-C during recording. These live steps require an available microphone and services.

Protocol reference: https://github.com/rhasspy/wyoming
Capture reference: https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html
