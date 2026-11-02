# Athena

Athena is a Raspberry Pi 5 voice assistant. OVOS captures and transcribes speech
using the existing reSpeaker, wake/VAD and local Whisper/Wyoming layers. Every
completed transcript now goes to a Pi-hosted agent that uses a configurable
cloud model and two independent MCP backends.

```text
reSpeaker -> OVOS wake + recording -> local Whisper -> Athena agent on Pi
                                                         |
                                 cloud model API <-------+-- MCP -> Home Assistant
                                                         +-- MCP -> Windows-MCP on PC
                                                         +-- native Wake-on-LAN
```

The Pi owns conversation state, tool selection/dispatch and result handling.
The cloud model does not connect directly to your LAN. Athena Desktop supervises
Windows-MCP in the signed-in Windows session; Home Assistant exposes its own MCP
integration. Both servers are reached through authenticated Streamable HTTP.

## Setup and run

Follow [agent setup](docs/AGENT_SETUP.md) for cloud model credentials, MCP URLs,
network configuration, image support and limits. The model/provider is chosen
explicitly: this branch does not create a cloud account, select a model, contact
live devices, or migrate credentials automatically.

On the Pi:

```bash
sudo apt-get install python3-venv libportaudio2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt
.venv/bin/python -m pip install -e '.[agent]'
.venv/bin/python -m athena_ovos.setup
```

Merge `.env.example` into your ignored `.env` and copy `agent.example.json` to
ignored `agent.local.json`, replacing example URLs/settings locally. Keep the
existing Wyoming Whisper service running (default `tcp://127.0.0.1:10300`).

```bash
./run.sh --check-agent-config  # settings only, no network or tool calls
./run.sh --text                # cloud agent, microphone-free
./run.sh --check-config        # existing voice plugin/wake-model checks
./run.sh                       # normal voice operation
```

The same agent handles voice and typed requests. Original deterministic REST
routing remains available only through `--legacy-text` for diagnostics. It is
not automatically tried after an uncertain model/MCP failure.

## Agent behavior

- Discover HA/Windows tool schemas, namespace names and validate arguments.
- Serialize requests and tool execution with bounded calls, rounds and history.
- Allow healthy backends to work when another MCP server is unavailable.
- Report unknown execution outcomes without retrying or continuing the batch.
- Forward bounded screenshots to a vision-capable cloud model when explicitly
  enabled; otherwise explain that the image was not forwarded.
- Keep `native__wake_pc` on the Pi so PC wake does not depend on Windows-MCP.
- Initialize model/MCP connections lazily; close owned sessions on shutdown.

Cloud requests include transcripts, recent conversation and selected tool
results. With `vision=true` they can include desktop screenshots. Conversation
is memory-only; prior-turn screenshots are discarded. Choose exposed tools and
provider settings accordingly. Spoken acknowledgments/TTS remain unfinished:
responses currently appear in the terminal and OVOS speak events.

## Voice and unfinished evaluation

The microphone, VAD, wake and transcription implementations are retained. The
current normal launcher uses the existing single-word **Athena** Precise ONNX
model. Custom **hey athena** openWakeWord collection/training/evaluation remains
opt-in; hardware accuracy testing is intentionally deferred until after this
architecture migration.

See [voice setup](voice/README.md), [wake evaluation](voice/WAKE_EVALUATION.md),
[training](voice/TRAINING.md), and [current training setup](voice/TRAINING_SETUP.md).
External datasets and ignored training workspaces are not moved or deleted by
this rebuild. A new worktree does not copy those ignored data directories.

## Development and validation

The architecture contract is in [ARCHITECTURE.md](docs/ARCHITECTURE.md).
Agent-only development can install `requirements-agent.txt` without OVOS and run:

```bash
python -m unittest discover -s tests -p 'test_agent*.py' -v
```

With full voice dependencies installed:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use fake model/MCP endpoints and microphone frames. The HTTP integration
uses the real pinned MCP SDK with two local fake servers and a fake cloud
endpoint, including pagination, authentication, image forwarding and uncertain
outcomes. Linux service tests and live Pi/microphone/device/cloud validation are
separate checks. No live PC power actions are performed by automated tests.

The retained [legacy integrations](integrations/README.md) document the previous
REST action paths. [Athena Desktop](https://github.com/b-stha/athena-desktop)
contains the Windows-MCP supervisor and explicit legacy command-server mode.
