# Voice frontend

Athena uses its custom openWakeWord classifier for wake detection. OVOS manages
microphone input, command recording, speech endpointing, STT coordination and
intent dispatch. The existing local Wyoming Whisper server transcribes completed
recordings; the Pi-hosted agent interprets the transcript and calls MCP tools.

Run on the Pi with the microphone attached. SSH does not forward the PC's
microphone, and voice mode does not need an interactive terminal.

For custom **Athena / Hey Athena** evaluation work, see
[recording, replay and buffered handoff](WAKE_EVALUATION.md) and
[training compatibility and data collection](TRAINING.md). The live adapter
uses the same openWakeWord inference implementation as the replay tools.
The [current training setup](TRAINING_SETUP.md) includes the ready-to-run Pi
recording command and the PC's persistent GPU pause control.

## Install

From the repository on the Pi:

```bash
sudo apt-get install python3-venv libportaudio2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt -r voice/requirements-wake.txt
.venv/bin/python -m pip install -e '.[agent,wake]'
```

Supply the trained classifier and its matching ONNX feature models explicitly.
Set these paths in the checkout's `.env`, using the actual files on the Pi:

```dotenv
ATHENA_WAKE_MODEL=/path/to/hey_athena.onnx
ATHENA_WAKE_MELSPEC_MODEL=/path/to/melspectrogram.onnx
ATHENA_WAKE_EMBEDDING_MODEL=/path/to/embedding_model.onnx
```

The default launcher selects `--wake-engine custom`. Missing models stop startup;
it does not download a model or fall back to the community Precise detector.
Models, recordings and generated training artifacts remain outside Git.

The selected October 31 candidate is
`E:\AthenaTraining\training\causal-positive-20261031\model\hey_athena.onnx`,
SHA256 `164734b986d894d4014b736e750a6760d65a8449e46ca0096480561d5bfb0caf`.
Its threshold is `0.98`, with one qualifying prediction and a two-second cooldown.
On the development validation session it detected 26/28 positive clips:
8/10 standalone Athena, 10/10 Hey Athena and 8/8 immediate-command clips. It
produced one false event in 111 negative seconds. The threshold was tuned on that
validation session; fresh held-out accuracy testing and a live command check
remain necessary. These replay results do not establish live pipeline accuracy.

Keep the existing Wyoming Whisper server running. Its default address is
`tcp://127.0.0.1:10300`, matching the Pi's current Whisper Docker service. Override
it in the repository's `.env` if necessary:

```dotenv
WHISPER_URI=tcp://127.0.0.1:10300
```

Cloud model, MCP token and Wake-on-LAN settings remain in `.env`. Configure
`agent.local.json` using [agent setup](../docs/AGENT_SETUP.md). Legacy `HA_URL`,
`HA_TOKEN` and `DESKTOP_URL` are used only by `--legacy-text` diagnostics. Athena
loads environment settings before starting OVOS services.
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

The frontend starts OVOS's message bus, core and listener together. For an initial
read-only command, say:

```text
Hey Athena, what time is it?
```

Check the transcript and agent result in the terminal or service journal.
There is no TTS service in this migration, so Athena does not speak its result.
Ctrl-C or SIGTERM stops the frontend and its managed OVOS processes.

Optional runtime overrides:

```bash
./run.sh --mic-device 'reSpeaker Flex XVF3800'
./run.sh --model /path/to/hey_athena.onnx \
  --melspec-model /path/to/melspectrogram.onnx \
  --embedding-model /path/to/embedding_model.onnx
./run.sh --bus-port 8182
```

The default local message-bus port is 8181. A replacement model must be compatible
with openWakeWord and trained for the phrase you intend to say. Renaming an
unrelated model does not change its wake phrase. The old `WAKE_URI`, `WAKE_WORD`
and `WAKE_PHRASE` settings do not select the detector.

For typed command debugging:

```bash
./run.sh --text
```

Text mode skips the microphone and wake model and uses the same cloud agent as voice. `--manual` and Space-controlled
recording are removed.

The skills process loads only Athena's skill through OVOS's native loader. Its
agent fallback registers during loading, so startup completes without waiting
for an intent-training acknowledgement or checking internet connectivity.
Cloud/MCP connections are initialized lazily when you issue a command.

## Custom wake threshold

The custom classifier uses fresh 80 ms predictions, with these initial settings:

```bash
./run.sh --wake-threshold 0.98 --wake-consecutive 1 --wake-cooldown 2
```

Higher thresholds and additional consecutive qualifying predictions can reduce
false activations while missing more real wakes. Model scores are not calibrated
probabilities that the wake phrase was spoken. Compare deliberate Athena and
Hey Athena wakes at the desk and intended distance with ordinary speech, typing
and room sound. Select settings on validation recordings and keep a separate
final test session.

Use flags on each launch. `.ovos/config/athena/mycroft.conf` is generated at
startup, so manual edits there do not persist through `./run.sh`.

### Explicit legacy Precise mode

The older Precise adapter remains available for comparison through an explicit
engine selection:

```bash
./run.sh --wake-engine precise --model /path/to/athena.onnx \
  --wake-sensitivity 0.2 --wake-trigger-level 5
```

Only this legacy mode uses `--wake-sensitivity` and `--wake-trigger-level`.
The legacy community model can be provisioned with
`.venv/bin/python -m athena_ovos.setup`; the custom mode does not use that model.

Legacy defaults remain sensitivity `0.5` and trigger level `3`. Lower sensitivity
makes Precise stricter: `0.2` requires a decoded model score above `0.8`, compared
with `0.5` at the default. Increasing the trigger level requires more qualifying
model predictions before activation. The counter decreases gradually when
predictions fall below the threshold; it does not require consecutive matches.

These settings are a starting point for testing with your voice and room sounds.
Stricter settings can also miss a real `Athena`. Check both background-noise false
activations and deliberate wake words before choosing values. The launcher
prints the effective settings and rejects invalid ranges before starting OVOS.

### Legacy Precise diagnostics

Enable score and input-level reports for a controlled test:

```bash
./run.sh --wake-engine precise --wake-sensitivity 0.2 --wake-trigger-level 5 --wake-diagnostics
```

After the 1.5-second warmup, `Wake diagnostic:` reports appear once per second
of audio processed while detecting the wake word, and immediately on a trigger.
They report the current decoded model score, the maximum since the previous
report, the threshold, qualifying frame counts and activation counts. A trigger
shows the count that caused activation rather than the detector's cooldown.
These scores are detector outputs, not a calibrated percentage certainty that
someone said Athena. Reports pause during command recording and transcription.

`rms_dbfs` and `peak_dbfs` describe the current rolling 1.5-second input window;
`window_ms` gives its length. Zero dBFS is full scale, more negative values are
quieter, and `-inf` means digital silence. The level window corresponds to the
current score; an earlier maximum score can have different levels. Diagnostics
retain only bounded numeric summaries and write no audio recordings.

Compare deliberate Athena detections at the desk and intended distance with
keyboard typing and phone/video speech that never says Athena. Keep the same
settings between examples. If unrelated audio scores as highly as your real
wake word, threshold tuning alone cannot separate those examples; evaluate a
better-trained Athena model. Diagnostics are disabled on launches that omit the
flag; launcher options regenerate the configuration each time.

## Microphone and recording

`athena_ovos.plugins.AthenaMicrophone` opens the reSpeaker Flex XVF3800 as
16 kHz, signed 16-bit stereo and selects physical **channel 2**. It supplies
640-byte, 20 ms, little-endian mono chunks to the OVOS listener, without mixing
the raw and processed channels.

The live custom adapter accumulates these chunks into fresh 80 ms openWakeWord
predictions. It counts only fresh scores and resets buffered inference state
after a consumed wake or recording gap, so unrelated audio segments are not
joined across commands. The microphone's physical channel 2 is zero-based
channel 1, matching the candidate's recorded training/evaluation channel.

OVOS's listener uses the selected custom wake plugin and records the following
command using WebRTC VAD. Its initial settings are:

| Setting | Value |
| --- | --- |
| Minimum speech to begin a command | 0.1 seconds |
| Silence to finish a command | 0.8 seconds |
| Maximum wait for speech after the wake word | 5 seconds |
| Maximum recording duration | 30 seconds |
| Audio retained around wake detection | 200 ms |

Athena's listener tracks whether OVOS confirms command speech by entering its
recording state after the minimum speech duration. If that never happens before
the five-second speech wait expires, Athena discards the buffered recording
without contacting Whisper and returns to wake detection. Recording-end cleanup
and the usual retry feedback still run. Rejected audio is cleared before the
next recording starts.

This uses the existing WebRTC speech decision; it adds no volume threshold.
Noise that WebRTC classifies as sustained speech can still reach transcription,
and quiet real speech that it misses will be discarded. Validate both keyboard
noise and normal speech from the intended distance before tuning sensitivity.

The microphone plugin supplies audio throughout the listener's lifecycle. Its
bounded queue retains recent audio while OVOS is busy transcribing. OVOS owns
the command buffer and decides when to start and finish recording; Athena does
not run a second microphone recorder.

`athena_ovos.plugins.AthenaWhisperSTT` receives the completed OVOS recording,
converts it to 16 kHz, signed 16-bit mono and peak-normalizes it to -1 dBFS before
sending Wyoming transcription events. It requests English, connects within
five seconds and limits each transcription request to 30 seconds by default.
Disconnects, server errors and timeouts are reported as failures.

OVOS pauses wake detection while waiting for transcription. When transcription
fails or returns no text, the terminal reports that you can try again and OVOS
returns to wake detection. The deadline starts after recording, so the time
spent speaking and waiting for end-of-speech is separate.

Override the transcription deadline with a finite positive number of seconds:

```bash
./run.sh --stt-timeout 30
```

A timeout closes Athena's connection; it does not cancel inference already
running inside the Whisper server. A slow server may still be busy when the next
request arrives. The shorter deadline bounds Athena's wait, while server
performance and recognition quality need a separate check.

Stopping Athena discards an unfinished recording and joins the framework's
buffer worker, without requesting transcription of that partial command.

## Agent requests

OVOS forwards every completed transcript to the Pi-hosted agent through Athena's
fallback handler. The agent uses the configured cloud model to select HA and
Windows MCP tools. The old Adapt language grammar is not in the normal pipeline.
`athena.context.request` marks submission and `athena.action.result` carries the
agent response or failure, preserving OVOS session context. Neither event alone
proves that an external device action completed.

A single worker serializes requests. It does not block the OVOS callback while
waiting for the model or tools. Full queues produce busy feedback. Shutdown
cancels the current turn, drops queued work and closes owned connections; remote
actions already accepted cannot be undone by cancelling the local request.
See [agent setup](../docs/AGENT_SETUP.md) for configuration, screenshot support,
limits and uncertainty handling.

## Testing

After installing the package:

```bash
.venv/bin/python -m unittest discover -s tests
```

Automated tests cover channel selection, audio format, the actual listener's
recording states and STT wrapper, Wyoming requests, connection cleanup, timeouts
and command handling through real OVOS core and message-bus processes.
Custom adapter tests cover fresh-score cadence, reset behavior and required
model configuration. The retained legacy Precise tests include a real-model
silence check when its pinned model is installed.
They use fake microphone frames and backend actions. They do not measure live
wake recognition or your room's speech endpointing behavior.

For a live check, start voice mode and try:

1. `Athena, open notepad` and confirm the PC opens it.
2. `Athena, turn on nanoleafs` and confirm the Home Assistant action.
3. Say `Athena` without a command and wait for the speech timeout.
4. Speak ordinary sentences without the wake word and check for false activation.
5. Try an ambiguous request and check that the model asks for clarification.
6. Stop Athena during listening with Ctrl-C and confirm its processes exit.

Once command delivery works, save your PC work before saying `Athena, sleep PC`.
Check the specific tool result: acceptance of a power request does not confirm
that Windows finished sleeping.

References: [openWakeWord](https://github.com/dscripka/openWakeWord),
[OVOS listener](https://github.com/OpenVoiceOS/ovos-dinkum-listener),
[Precise ONNX plugin](https://github.com/OpenVoiceOS/ovos-ww-plugin-precise-onnx),
[published wake models](https://github.com/OpenVoiceOS/precise-lite-models),
[Wyoming](https://github.com/rhasspy/wyoming).
