# Voice setup

Say "Athena" or "Hey Athena", then give your command. The Pi listens for the
wake phrase, records the command, and turns it into text with local Whisper.
After that, voice and typed commands follow the same path.

The wake detector uses openWakeWord. OVOS handles microphone recording and
speech timing. Replies currently appear as text, without a spoken response.

## Set up the Pi

Start with the [main setup guide](../readme.md). Connect a capture microphone,
install `libportaudio2`, and keep the local Whisper service running.
Set these paths in the checkout's `.env`:

```dotenv
ATHENA_WAKE_ENGINE=custom
ATHENA_WAKE_MODEL=/path/to/hey_athena.onnx
ATHENA_WAKE_MELSPEC_MODEL=/path/to/melspectrogram.onnx
ATHENA_WAKE_EMBEDDING_MODEL=/path/to/embedding_model.onnx
WHISPER_URI=tcp://127.0.0.1:10300
```

Supply the three model files separately. The [model file list](custom-wake-model.json)
records which versions are used. Recordings and model files stay outside Git.

The current Pi uses a reSpeaker Flex XVF3800 microphone, taking physical
channel 2 at 16 kHz with int16 samples. The [Whisper build](whisper/Dockerfile)
and [settings example](whisper/runtime.env.example) describe its speech service.
The service needs its model and tokenizer saved locally for offline use.

## Run and check

The configured Pi normally runs voice in the background:

```bash
systemctl --user status athena-agent.service
journalctl --user -u athena-agent.service -f
```

For a foreground voice test, stop the background listener first:

```bash
systemctl --user stop athena-agent.service
./run.sh
# Press Ctrl+C when finished.
systemctl --user start athena-agent.service
```

Run `./run.sh --text` to test command handling without the microphone.
Run `./run.sh --check-config` to check voice setup without recording.
An SSH session uses the Pi's microphone, not your PC's.

## Timing and troubleshooting

Athena allows five seconds for command speech to begin after waking.
Two seconds of silence ends a command. Change `ATHENA_COMMAND_WAIT` and
`ATHENA_SILENCE_END` in `.env` if needed.

Before transcription, Athena trims silence around the recording while keeping
all detected speech and the pauses between phrases. It waits for queued audio
to finish buffering so the end of a command is included.

The background service warms up Whisper before listening. To do that separately:

```bash
./run.sh --warmup-stt --stt-timeout 15
```

This sends test silence, without recording or executing a command.
Set `ATHENA_STT_DIAGNOSTICS=true` to log transcription time and microphone
levels without saving audio.

Wake detection and transcription still need real-room testing. A false wake
can capture unrelated speech, which may then reach the cloud model.
For wake sensitivity settings, use `./run.sh --help`; the selected values
are also in the model file list. Wake scores are not accuracy percentages.

## Training and development

Training is paused. Recording, training, and evaluation tools live in the
separate sibling `athena_tools/voice_lab/` project. They are not needed to run
Athena. Earlier experiments and results remain there for reference.

The dated requirements lock here covers voice inference only.
The older Precise detector is available explicitly through `--wake-engine precise`.
Core tests check the voice connections with fake audio; they do not measure
how reliably Athena understands speech from across a room.
