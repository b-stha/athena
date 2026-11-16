# Voice runtime

Athena's custom openWakeWord detector activates the OVOS listener. OVOS records
the command and sends it to local Wyoming Whisper. Voice and typed commands
then share the local-first dispatcher; contextual requests use the cloud model.
Responses currently appear as text in the terminal or service journal.

## Setup

Follow the [core setup](../readme.md). The Pi also needs `libportaudio2` and a
connected capture microphone. Configure these paths in the checkout's `.env`:

```dotenv
ATHENA_WAKE_ENGINE=custom
ATHENA_WAKE_MODEL=/path/to/hey_athena.onnx
ATHENA_WAKE_MELSPEC_MODEL=/path/to/melspectrogram.onnx
ATHENA_WAKE_EMBEDDING_MODEL=/path/to/embedding_model.onnx
WHISPER_URI=tcp://127.0.0.1:10300
```

The [model manifest](custom-wake-model.json) records the selected assets,
channel and wake settings. Models are supplied separately; custom mode does
not download a replacement. Model weights, recordings and training outputs
stay outside Git.

The live microphone uses the reSpeaker Flex XVF3800 at 16 kHz/int16, selecting
physical channel 2 and supplying mono audio to the listener. Keep the existing
Whisper service running. The [speech image](whisper/Dockerfile) and its
[settings example](whisper/runtime.env.example) describe the pinned runtime build.
The current deployment uses int8 inference, four threads and beam size 3.
Offline mode requires the model and fallback tokenizer in the persistent cache.

## Run and inspect

From the core repository:

```bash
./run.sh --check-config
./run.sh --text
```

The configuration check opens no microphone and sends no command. Text mode
uses the same dispatcher without wake detection or transcription.

The provisioned Pi runs voice through its enabled user service:

```bash
systemctl --user status athena-agent.service
journalctl --user -u athena-agent.service -f
```

For a foreground voice session, stop that service before running `./run.sh`.
Restart the service when finished. Two listeners cannot share the microphone.
SSH does not forward your PC microphone.

## Current controls

| Setting | Default |
| --- | --- |
| Wake score threshold | 0.98 |
| Fresh predictions needed | 1 |
| Wake cooldown | 2 seconds |
| Wait for command speech | 5 seconds |
| Silence to finish a command | 2 seconds |
| Transcription timeout | 30 seconds |

Set `ATHENA_WAKE_THRESHOLD`, `ATHENA_WAKE_CONSECUTIVE`, `ATHENA_WAKE_COOLDOWN`,
`ATHENA_COMMAND_WAIT` and `ATHENA_SILENCE_END` in `.env`; CLI overrides are listed
by `./run.sh --help`. Wake scores are not calibrated probabilities.

`ATHENA_STT_DIAGNOSTICS=true` logs transcription time and audio-level summaries
without saving recordings. A wake with no confirmed command speech is discarded
before transcription. Speech errors and false activations still need monitoring.

The service primes Whisper before listening. To check warm-up separately:

```bash
./run.sh --warmup-stt --stt-timeout 15
```

This sends synthetic silence and discards the text; it opens no microphone or
agent. Single-pass decoding is controlled by `ATHENA_WHISPER_SINGLE_PASS` in the
Whisper container's environment.

## Development

Inference requirements and the Whisper build files in this directory support
deployment. The dated lock is a standalone inference snapshot, not the full
OVOS application's lock. Legacy Precise mode remains an explicit diagnostic
option through `--wake-engine precise`.

Recording, training, replay tools and archived evaluation notes live in the
separate sibling `athena_tools/voice_lab/` project. Training remains paused.
The former long guide and model-selection results are preserved in that lab's
`docs/history/` directory.

Core tests use fake audio and backends:

```bash
.venv/bin/python -m unittest discover -s tests
```

They verify integration behavior; room-distance speech accuracy requires live
testing.
