# Voice frontend

OVOS manages Athena's microphone input, wake detection, recording, speech
endpointing, STT coordination and intent dispatch. The existing local Whisper
server performs transcription. Athena supplies microphone and Wyoming STT
plugins plus a small adapter for OVOS's Precise detector. The previous standalone
recording and openWakeWord loop is retired.

Run on the Pi with the microphone attached. SSH does not forward the PC's
microphone, and voice mode does not need an interactive terminal.

## Install

From the repository on the Pi:

```bash
sudo apt-get install python3-venv libportaudio2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
.venv/bin/python -m athena_ovos.setup
```

The setup command downloads a pinned version of the existing community
single-word **Athena** Precise ONNX model into `.ovos`. No `athena.tflite` or
custom openWakeWord training is needed. This is an existing model whose
recognition accuracy with your voice, microphone and room still needs a live
check.

Keep the existing Wyoming Whisper server running. Its default address is
`tcp://127.0.0.1:10300`, matching the Pi's current Whisper Docker service. Override
it in the repository's `.env` if necessary:

```dotenv
WHISPER_URI=tcp://127.0.0.1:10300
```

Backend settings such as `HA_URL`, `HA_TOKEN`, `DESKTOP_URL` and the Wake-on-LAN
configuration remain in `.env`. Athena loads them before starting OVOS services.
OVOS configuration and caches are isolated under `.ovos`, leaving other OVOS
installations' settings separate.

## Validate and start

```bash
./run.sh --check-config
./run.sh
```

`--check-config` verifies configuration, plugin loading and the wake model
without opening the microphone or sending device commands. It does not verify
the Whisper service, physical microphone or wake recognition accuracy.

The frontend starts OVOS's message bus, core and listener together. Say:

```text
Athena, open notepad
```

Check the transcript and result in the terminal and confirm that Notepad opens on the PC.
There is no TTS service in this migration, so Athena does not speak its result.
Ctrl-C or SIGTERM stops the frontend and its managed OVOS processes.

Optional runtime overrides:

```bash
./run.sh --mic-device 'reSpeaker Flex XVF3800'
./run.sh --model /path/to/another-athena.onnx
./run.sh --bus-port 8182
```

The default local message-bus port is 8181. A replacement model must be compatible
with Precise ONNX and trained for the phrase you intend to say. Renaming an
unrelated model does not change its wake phrase. The old `WAKE_URI`, `WAKE_WORD`
and `WAKE_PHRASE` openWakeWord settings no longer select the detector.

For typed command debugging:

```bash
./run.sh --text
```

Text mode skips the voice frontend and its model. `--manual` and Space-controlled
recording are removed.

## Microphone and recording

`athena_ovos.plugins.AthenaMicrophone` opens the reSpeaker Flex XVF3800 as
16 kHz, signed 16-bit stereo and selects physical **channel 2**. It supplies
640-byte, 20 ms, little-endian mono chunks to the OVOS listener, without mixing
the raw and processed channels.

The Precise adapter accumulates these chunks into the model's 50 ms hops and
waits for 1.5 seconds of audio to fill its feature history before allowing a wake
activation. It avoids counting repeated predictions between feature updates.
Allow that warmup after startup or a consumed wake activation. OVOS's Precise
engine still performs the model inference.

OVOS's listener performs wake detection using Precise ONNX and records the
following command using WebRTC VAD. Its initial settings are:

| Setting | Value |
| --- | --- |
| Minimum speech to begin a command | 0.1 seconds |
| Silence to finish a command | 0.8 seconds |
| Maximum wait for speech after the wake word | 5 seconds |
| Maximum recording duration | 30 seconds |
| Audio retained around wake detection | 200 ms |

The microphone plugin supplies audio throughout the listener's lifecycle. Its
bounded queue retains recent audio while OVOS is busy transcribing. OVOS owns
the command buffer and decides when to start and finish recording; Athena does
not run a second microphone recorder.

`athena_ovos.plugins.AthenaWhisperSTT` receives the completed OVOS recording,
converts it to 16 kHz, signed 16-bit mono and peak-normalizes it to -1 dBFS before
sending Wyoming transcription events. It requests English, connects within
five seconds and limits each transcription request to 120 seconds. Disconnects,
server errors and timeouts are reported as failures.

Stopping Athena discards an unfinished recording and joins the framework's
buffer worker, without requesting transcription of that partial command.

## Intents and contextual requests

OVOS passes recognized speech to Athena's deterministic intent handlers. Those
handlers validate their targets and invoke the existing Home Assistant, desktop
HTTP or Wake-on-LAN integration. A recognized command that fails execution is
reported as a failure.

Requests that need context can reach `athena.context.request` on the message bus.
The contextual reasoning and MCP consumer is a future component; this version
does not execute a contextual fallback or send unmatched speech to a cloud LLM.

## Testing

After installing the package:

```bash
.venv/bin/python -m unittest discover -s tests
```

Automated tests cover channel selection, audio format, the actual listener's
recording states and STT wrapper, Wyoming requests, connection cleanup, timeouts
and command handling through real OVOS core and message-bus processes.
When the pinned model is installed, a regression check runs actual model
inference on synthetic silence; otherwise that check is skipped with setup
instructions.
They use fake microphone frames and backend actions. They do not measure live
wake recognition or your room's speech endpointing behavior.

For a live check, start voice mode and try:

1. `Athena, open notepad` and confirm the PC opens it.
2. `Athena, turn on nanoleafs` and confirm the Home Assistant action.
3. Say `Athena` without a command and wait for the speech timeout.
4. Speak ordinary sentences without the wake word and check for false activation.
5. Try an unsupported request and check that no device action executes.
6. Stop Athena during listening with Ctrl-C and confirm its processes exit.

Once command delivery works, save your PC work before saying `Athena, sleep PC`.
The desktop client acknowledges the request before applying the power action;
an acknowledgment does not confirm that Windows finished sleeping.

References: [OVOS listener](https://github.com/OpenVoiceOS/ovos-dinkum-listener),
[Precise ONNX plugin](https://github.com/OpenVoiceOS/ovos-ww-plugin-precise-onnx),
[published wake models](https://github.com/OpenVoiceOS/precise-lite-models),
[Wyoming](https://github.com/rhasspy/wyoming).
