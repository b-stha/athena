# Voice input

Athena listens for the single word **Athena**, records the following command,
and stops after 0.8 seconds of silence. Wake detection uses a local Wyoming
openWakeWord service. Whisper receives only the resulting command recording.
Run on the machine with the microphone attached; SSH does not forward your
laptop microphone. Default voice mode does not require an interactive terminal.

## Setup

First train an openWakeWord model for `athena` and save the TFLite output as
`voice/models/athena.tflite`. See [custom model setup](models/README.md).
Changing a model filename does not change the phrase it recognizes. A model for
"Hey Athena" cannot be substituted for the single-word model.

```bash
sudo apt-get install libportaudio2
.venv/bin/python -m pip install -r requirements.txt
docker compose -f voice/compose.yaml up -d
.venv/bin/python main.py
```

Say `Athena, turn on nanoleafs`. Audio around the detection is retained so a
command spoken immediately after the word is not intentionally cut off. After
detection, Athena waits up to five seconds for speech. Recordings with less than
100 ms of speech are discarded; commands have a 30-second limit. WebRTC VAD checks
raw audio for speech before normalization. The microphone closes during
transcription and command execution, then reopens to listen for another wake word.

Athena prints the original Whisper transcript
and resolves its target before passing the command to the router. Say `turn on nanoleafs` to operate
`light.nanoleafs`. Only a leading `Athena` is removed from the transcript before
resolution. Ctrl-C or SIGTERM stops the program and closes audio resources.
`.venv/bin/python main.py --text` retains typed input. For the previous keyboard
controls, use `.venv/bin/python main.py --manual` from an interactive Linux terminal:
press and release Space to start speaking, then press Space again to stop.

The default wake endpoint is `tcp://127.0.0.1:10400`; `voice/compose.yaml` binds it
only to the Pi's loopback interface and mounts the model directory read-only.
The service must be started separately. It does not start automatically with Athena.
Set these in `.env` to use another endpoint or model:

```dotenv
WAKE_URI=tcp://127.0.0.1:10400
WAKE_WORD=athena
WAKE_PHRASE=Athena
```

`WAKE_WORD` is the model ID reported by the service, normally the TFLite filename
without its extension. `WAKE_PHRASE` is the spoken text removed from the transcript;
its default is the model name with underscores replaced by spaces. Athena checks
the service's model list before opening the microphone and exits with an error if
the selected model is missing. It never silently switches to a built-in wake word.
Restart the wake service after adding or replacing a model.

Whisper must already be running as a Wyoming service. The default endpoint is
`tcp://127.0.0.1:10300`, matching the port in `/home/MK/docker/whisper/compose.yaml`.
Set `WHISPER_URI` in `.env` if it runs elsewhere. The client does not start Docker
or load a model. Requests use English and have a 120-second overall timeout.

`audio.py` defines the 16 kHz audio format and selects physical channel 2 from
the first two channels of the reSpeaker Flex XVF3800 as 16-bit mono PCM.
`wake.py` streams this channel to wake detection and uses a bounded in-memory
buffer for automatic command recording. Capture failures or buffer overflow
discard the recording. `input.py` provides the optional Space-controlled recorder.
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
The automated checks use fake audio, wake events and integrations; they do not
verify the trained model's recognition accuracy. To verify hardware after installing
the model, start the program, say `Athena, turn on nanoleafs`, and check the displayed
transcript and light response. Also try the wake word without a command, ordinary
speech without the wake word, an unsupported command, and Ctrl-C during recording.
These live steps require an available microphone and services. Tune and retrain
the wake model if it misses activations or triggers on background speech.

Protocol reference: https://github.com/rhasspy/wyoming
Capture reference: https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html
Wake service: https://github.com/rhasspy/wyoming-openwakeword
Speech detection: https://github.com/wiseman/py-webrtcvad

Scoring reference: https://rapidfuzz.github.io/RapidFuzz/Usage/distance/Levenshtein.html
