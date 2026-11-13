# Athena

Athena is a Raspberry Pi voice and text assistant. It uses a custom
openWakeWord detector, OVOS capture/VAD and local Wyoming Whisper transcription.
Completed commands reach a shared dispatcher: registered routine actions execute
locally; contextual requests use a configured cloud model and MCP tools.

```text
voice -> local wake/capture/Whisper --+
typed command ----------------------+-> planner -> deterministic HA/Windows actions
                                              +-> contextual model -> MCP tools
```

## Repository layout

| Location | Purpose |
| --- | --- |
| `src/athena_agent/` | Runtime planning, local execution, cloud/MCP loop and tracking |
| `src/athena_ovos/`, `src/athena_skill/` | Live audio adapters, OVOS services and transcript bridge |
| `src/athena_wake/` | Live detector and threshold/sample-clock logic |
| `src/router.py`, `src/resolver.py`, `src/integrations/` | Registered actions, fuzzy targets, Wake-on-LAN and legacy diagnostics |
| `tests/` | Runtime regression tests and fixtures |
| `voice/` | Runtime voice setup, selected model profile and inference pins |
| `docs/` | Local, ignored documentation; no longer versioned |
| `../athena_tools/` | External developer tools; not part of this repository |

Recording, synthesis, feature preparation, training, replay, speech benchmarks and
the experimental buffered frontend are in the separate
voice lab at `../athena_tools/voice_lab`. They are excluded from the runtime wheel.
Existing datasets and models remain in their external workspace. The core has no import dependency on those external developer tools.

## Install and configure

On the Pi, from this checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt -r voice/requirements-wake.txt
.venv/bin/python -m pip install -e '.[agent,wake]'
```

Copy `.env.example` and `agent.example.json` into ignored `.env` and
`agent.local.json`, or merge their settings into existing private configuration.
Configure the cloud endpoint/model, HA and Windows MCP bearer tokens, and the
three custom wake-model paths. Installation does not train/download a custom wake
model. Configure the existing local Whisper service separately.

The public configuration examples describe endpoint and secret settings.
[Voice setup](voice/README.md) covers microphone/runtime options; the selected
[model profile](voice/custom-wake-model.json) records asset hashes and settings.
The existing provisioned Pi's private `~/.local/bin/athena` launcher also loads
`~/.config/athena/secrets.env`; generic `run.sh` loads only the checkout `.env`.
Keep secrets, recordings, weights, virtual environments and model outputs out of Git.

## Run

```bash
./run.sh --check-config
./run.sh --check-agent-config
./run.sh --text
./run.sh
```

Enable the existing local backends for one process:

```bash
ATHENA_LOCAL_HA_ENABLED=true ATHENA_LOCAL_APPS_ENABLED=true ATHENA_ROUTE_LOG=true ./run.sh --text
```

Omit `--text` for voice. Stop a separately running voice service before starting
another foreground listener on the same microphone. Runtime changes in this
checkout do not automatically update another checkout or restart an installed service.

## Current behavior

- Registered HA light on/off actions call HA MCP directly, with local acknowledgement
  and no model requests. Both paths share the `ha` MCP endpoint/token.
- Registered Notepad launches use fixed Windows-MCP arguments without model calls.
- Two or three complete supported clauses execute in order after read-only
  whole-plan preflight: `turn on nanoleafs and open Notepad`.
- Fuzzy correction applies only to bounded registered target names, preserving
  action words and scope. Ambiguous matches ask for clarification.
- Mixed contextual requests, unresolved references/conditions and unsupported
  backends retain their whole request for the cloud agent. Clause-by-clause
  mixed local/cloud execution is not implemented.
- Per-request and per-tool outcomes survive errors and cancellation. Reusing a
  retained request ID replays its result without another effect. The 128-request
  ledger is process-local and is not durable across restarts or eviction.
- Acceptance is separate from physical completion; uncertain actions are not
  automatically retried or redirected through another transport.
- Responses are visible in the terminal/journal. A TTS service is not implemented.

Additional architecture/research notes may exist locally under ignored `docs/`.
They are not runtime dependencies or automatically supplied to the LLM.

The Windows repository is the signed-in desktop supervisor for Windows-MCP; the
Pi is its MCP client. See [Athena Desktop](https://github.com/b-stha/athena-desktop).
Explicit `--legacy-text` diagnostics remain available for HA and Wake-on-LAN.
Their Desktop app/power HTTP requests require a Desktop version from before the
legacy backend was removed; current Desktop uses Windows-MCP. See
[legacy integration scope](src/integrations/README.md). The Precise wake compatibility
adapter remains available independently of the Desktop transport.

## Verify and develop

With runtime dependencies installed, run the runtime regressions from this root:

```bash
python -m unittest discover -s tests -v
```

These tests use fake models/devices or loopback HTTP; they do not require paid
model requests or real device effects. Optional dependency cases can be skipped
by the test runner. The independent lab has its own installation and test suite.

[Developer tools](../athena_tools/README.md) documents the routing benchmark and new lab CLI
namespace. Lab installation/testing is separate from starting Athena; model
training remains subject to the external workspace's persistent pause control.

Historical measurements, account-specific cost assumptions and earlier designs
are preserved in [history](docs/history) and the lab's documentation rather than
presented as current guarantees. No cleanup changes promote a different model.
