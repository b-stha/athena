# Athena

Athena runs on a Raspberry Pi and controls Home Assistant and Windows through
voice or text. Familiar commands can run without a model request; contextual
requests use a cloud model and MCP tools.

Current support includes light controls, Notepad launching, and short compound
commands such as “turn on nanoleafs and open Notepad.” Broader application
discovery and graceful closing are planned. Voice recognition is still being
improved; responses currently appear as text.

## Setup

From the Pi checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt -r voice/requirements-wake.txt
.venv/bin/python -m pip install -e '.[agent,wake]'
```

Copy `.env.example` to `.env` and `agent.example.json` to `agent.local.json`.
Fill in your model connection, MCP tokens, and wake-model paths. Keep the local
Whisper service running. See [voice setup](voice/README.md) for microphone and
model configuration.

Enable model-free registered commands with `ATHENA_LOCAL_HA_ENABLED=true` and
`ATHENA_LOCAL_APPS_ENABLED=true` in `.env`.

## Use

```bash
./run.sh --text   # typed commands
./run.sh          # voice commands
```

Stop an existing voice listener before starting another. The provisioned Pi's
`~/.local/bin/athena` launcher also loads its private secrets file.

## Development

`src/` contains the runtime, `voice/` holds setup/model information, and `tests/`
contains regression checks. Offline benchmarks and the voice lab live outside
this repo in the sibling `athena_tools/` folder. Local `docs/` are ignored.

```bash
.venv/bin/python -m unittest discover -s tests
```

Windows companion: [Athena Desktop](https://github.com/b-stha/athena-desktop).
