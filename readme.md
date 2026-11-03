# Athena

Athena is a Raspberry Pi 5 voice assistant. Its trained openWakeWord model detects
Athena or Hey Athena; OVOS handles reSpeaker capture, VAD and recording, with local
Whisper/Wyoming transcription. Completed transcripts reach a Pi-hosted dispatcher.
An opt-in route handles registered HA light commands locally; other requests use
a configurable cloud model and two independent MCP backends.

```text
reSpeaker -> custom wake + OVOS recording -> local Whisper -> Athena dispatcher on Pi
                                                         |
                                 cloud model API <-------+-- MCP -> Home Assistant
                                                         +-- MCP -> Windows-MCP on PC
                                                         +-- native Wake-on-LAN
                              opt-in single HA action ---+-- direct HA REST
```

The Pi owns conversation state, tool selection/dispatch and result handling.
The cloud model does not connect directly to your LAN. Athena Desktop supervises
Windows-MCP in the signed-in Windows session; Home Assistant exposes its own MCP
integration. Both servers are reached through authenticated Streamable HTTP.

## Current state and architectural findings

Status recorded **2026-11-01**, on `codex/agent-mcp-rebuild` in both Athena and
Athena Desktop. The MCP architecture and opt-in single-HA local route are
implemented. General mixed local/contextual execution remains planned.
Existing voice training/testing work has been committed and integrated.

| Area | Current behavior | Remaining work |
| --- | --- | --- |
| Understanding | Voice/text share a dispatcher: opt-in single HA commands locally, other requests through the cloud agent. | Add scope-aware mixed execution and broader routine coverage. |
| Device access | HA MCP, Windows-MCP and Pi-native Wake-on-LAN are connected. | Share typed actions between rules, local parsing and model-selected plans. |
| Cost | Direct local commands use no model requests; cloud turns can invoke the model repeatedly with the full catalog. | Record actual usage, select relevant tools and remove unnecessary model rounds. |
| Voice | Custom Athena/Hey Athena openWakeWord, OVOS capture/VAD and local Whisper. Spoken clock queries succeeded. | Improve quiet/distant recognition, false activations, transcription and command handoff. |
| Desktop | Athena Desktop supervises pinned Windows-MCP; shared physical/AI input is configured. | Prefer background APIs where possible; foreground input still shares cursor/focus. |

The provisioned cloud model is Gemini 3.6 Flash with low reasoning effort and
vision enabled. This is standard transcript/screenshot request-response
inference. Gemini 3.8 Flash returned overload/timeouts during commissioning.
The tested project also reached a 20-request/day free-tier quota for 3.6 Flash;
one user command can consume multiple model requests.

The closed command grammar also feeds an opt-in local dispatcher. With
`ATHENA_LOCAL_HA_ENABLED=true`, one complete registered HA light on/off command
uses direct REST and a local acknowledgment, without cloud credentials or MCP.
The default flag is false; other requests use the cloud agent. `--legacy-text`
still selects the older diagnostic route, including its legacy Desktop transport.

## Active development priorities

The two active workstreams are **speech reliability** and **a scoped local /
contextual action planner**. The agreed next design keeps HA MCP as a last-resort
contextual route when local intent resolution is insufficient. A local backend
timeout is an execution failure, not a reason to repeat the action through MCP.
Unintelligible speech can require clarification instead of a paid model request.

For speech, assess the custom OWW detector independently from capture/VAD and
Whisper. The Pi currently advertises faster-whisper **`base-int8`**. Compare the
same recorded command sent directly to Whisper and through the OVOS handoff
before deciding which component to replace. Quiet-speech misses and false wakes
require separate measurements; reducing the classifier threshold trades one
against the other.

For routing, first define the supported typed actions, aliases, context rules,
compound-command semantics and cloud budget. Routine actions should execute
and acknowledge locally even when cloud credentials/quota are unavailable.
Contextual fallback should receive only relevant tools and verified action
history. The acceptance criteria and implementation sequence are in the
[development plan](docs/DEVELOPMENT_PLAN.md). These are planned changes; normal
Mixed and contextual runtime requests still use the existing cloud agent.

The first [command-planning preview](docs/COMMAND_PLANNING.md) is implemented:
`python -m athena_agent.planning 'turn on bedroom lights and open my most recent project'`.
It exposes typed local candidates and unresolved contextual clauses with source
spans and ordering dependencies. The preview itself executes nothing. The first
opt-in runtime integration handles single HA actions and records outcomes;
scope-aware mixed execution remains outstanding. See the linked testing steps.

Opt-in route diagnostics (`ATHENA_ROUTE_LOG=true`) now identify local/cloud/
clarification decisions, reasons, outcomes and model-attempt counts without
logging transcript content. The [43-case routing baseline](docs/ROUTING_BASELINE_20261102.md)
records current coverage and development gaps using fake backends, with no
device effects or paid model calls. Routing rules are unchanged by that benchmark.
The [phrasing follow-up](docs/LIGHT_PHRASING_20261102.md) now resolves all six
ordinary light-command misses locally, reducing cloud routes from 30 to 24 on
the same authored 43-case suite. This is development coverage, not measured
real-world usage or billing savings.

## Planned local and contextual boundary

The unit of routing should be an **action or clause**, rather than the whole
utterance. Deterministic execution means validated typed actions; contextual
language interpretation can still be used to produce those actions.

```text
Voice / text -> preserve full request and identify action clauses
                 | explicit target and supported operation -> local action
                 | context, ambiguity or screen reasoning -> LLM with relevant tools
                 +--------------------------+--------------------------+
                                            v
                                  shared validated executor
                                            v
                                    HA / Pi / Windows

Structured events / schedules -> conditions and rules -> same executor
```

For **"Turn on the lights and open my most recent project"**, the light action
can run locally if its room/group is configured unambiguously. The remaining
project-opening action needs context. Give the LLM the relevant original request,
project context and the light action's result, marking that action as completed
so it is not repeated. If "the lights" has no clear target, resolve or clarify it
before changing devices.

Do not split mechanically on "and". Negation, conditions, ordering, corrections
and references can span clauses: "don't turn on the lights and open the editor,"
"if I'm home, turn on the lights and open my project," and "turn it off after
that finishes" need their scope and dependencies preserved. Parse the complete
meaning before committing dependent actions; independent, validated local steps
can proceed while contextual steps are resolved.

Local and model-generated steps should share action IDs, execution results,
session context and a completion ledger. Carry forward successful, failed and
unknown outcomes; do not replay a completed or uncertain action through a
different path. Model failure may leave a mixed request partially complete, and
the response must report that accurately.

Use Home Assistant automations/scripts for ordinary home events. Keep PC wake
on the Pi, since the Windows server cannot run while the PC is off. Fixed power
and application actions should use named validated Windows operations; broader
desktop investigation can use the agent and Windows-MCP. Trigger/rule ownership
is separate from tool transport and does not require another MCP server.

## Model costs and the HA integration decision

The earlier **$20-$40/month for 50 commands/day** was a planning estimate for
sending every command through the current cloud loop, not observed billing or a
required cost of MCP. Actual usage accounting is not yet implemented: the model
adapter currently discards provider token-usage metadata.

On 2026-11-01, a read-only tokenizer check of serialized system instructions and
the available tool definitions gave these **proxies**, not billed prompt counts:

| Catalog | Token proxy |
| --- | ---: |
| 33 tools: HA, Windows and native wake, plus system instructions | 7,013 |
| Same catalog with HA definitions removed | 5,606 |
| Difference | 1,407 |

The current loop resends these definitions on each model round. Conversation,
tool results, screenshots and output/thinking tokens add more usage. Provider
encoding, caching and actual task mix can change the bill.

For Gemini 3.6 Flash, published standard prices checked on this date are
$0.75/M input and $3.75/M output tokens, including thinking, through December 31,
2026; $1.50/M and $7.50/M respectively starting January 1, 2027.
See [Google pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.6-flash).

| Change | Expected effect |
| --- | --- |
| Replace HA MCP with direct REST but keep the same model/tool loop | No inherent reduction in model request count; compact tool definitions may reduce input. |
| Omit HA definitions from requests that do not need them | Removes that catalog overhead; the measured difference is about 20% of the catalog/system proxy, not 20% of the entire bill. |
| Resolve a routine HA command locally and produce its acknowledgment locally | Zero model requests and model-token cost for that command. Either direct HA APIs or programmatic MCP calls can execute it. |
| Have the model produce a terminal typed action/plan, then execute and acknowledge locally | Can avoid a final model response round when no further reasoning is needed. |
| Keep model follow-ups for searches, screenshots, failures and dependent decisions | Preserves the feedback needed for accurate contextual work. |

For scale only: at 1,500 commands/month and exactly two model calls per command,
removing 1,407 uncached input tokens per call would save about **$3.17/month** at
the current input rate, or $6.33 at the announced January rate. This assumes the
proxy difference represents actual input and that no replacement schema is sent;
it is not a savings guarantee. Replacing HA with a compact custom tool would
still have its own schema overhead.

MCP requests execute device tools; they are not themselves Gemini inference
requests. The application chooses whether to call the model again after a tool
result. A simple successful light action can use a local response template.
An investigation such as finding the most recent project may require additional
model/tool rounds regardless of whether the transport is MCP or REST.

Priority is therefore local routine actions, relevant-tool selection, bounded
context, optional local acknowledgments, and per-command usage/latency metrics.
The direct HA helper can be reused and hardened behind the shared executor;
removing HA MCP is an option, not a prerequisite for achieving those savings.
See the provider's [function-calling documentation](https://ai.google.dev/gemini-api/docs/function-calling)
and [token-counting documentation](https://ai.google.dev/gemini-api/docs/tokens).

## Setup and run

Follow [agent setup](docs/AGENT_SETUP.md) for cloud model credentials, MCP URLs,
network configuration, image support and limits. The model/provider is chosen
explicitly: this branch does not create a cloud account, select a model, contact
live devices, or migrate credentials automatically.

On the Pi:

```bash
sudo apt-get install python3-venv libportaudio2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt -r voice/requirements-wake.txt
.venv/bin/python -m pip install -e '.[agent,wake]'
```

Merge `.env.example` into your ignored `.env` and copy `agent.example.json` to
ignored `agent.local.json`, replacing example URLs/settings locally. Keep the
existing Wyoming Whisper service running (default `tcp://127.0.0.1:10300`).
Set the three `ATHENA_WAKE_*MODEL` paths to the trained classifier and matching
feature assets. See the [selected custom model](docs/VOICE_MODEL.md). The normal
launcher requires these assets; it does not select a Precise model if they are missing.

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

The normal launcher uses the trained dual-form **Athena / Hey Athena** openWakeWord
classifier. The older community Precise model requires explicit `--wake-engine
precise` selection. Collection/training/evaluation sources and external artifacts
are preserved. The selected candidate is tuned on development validation;
fresh room/voice accuracy testing remains unfinished.

Current settings are threshold `0.98`, one fresh qualifying prediction and
two seconds cooldown. This threshold is a classifier score, not a microphone
volume cutoff. The wake adapter has no explicit amplitude gate. The reported
need to speak loudly needs microphone/channel/level and model-data evaluation;
blindly lowering the score threshold can also increase false activations.

The latest development result is 26/28 positive clips, with one false event in
111 negative seconds; the threshold was tuned on those same recordings. It is
not a fresh accuracy result or a reliable hourly false-activation estimate.
Reports of false wakes, quiet-speech misses and inaccurate STT remain open.
Treat wake detection, capture/VAD and transcription as separate stages when
diagnosing them; a more permissive wake score alone cannot repair STT.

End-of-utterance silence is now two seconds, with a five-second initial speech
wait, configurable through `ATHENA_SILENCE_END` / `ATHENA_COMMAND_WAIT` or CLI
flags. The retained OVOS loop has about 200 ms rewind; some development wake
delays exceeded that, so immediate-command beginnings can still be clipped.
TTS is not implemented; transcripts and responses are visible in the journal.

The [2026-11-01 recorded speech comparison](docs/SPEECH_BASELINE_20261101.md)
ran eight existing command clips through the actual OVOS recording loop and
local Whisper. All eight activated; exact command transcriptions were 6/8 for
full recordings and 5/8 for retained audio. Longer rewind stayed at 5/8 and
disabling final VAD trimming fell to 2/8, so live settings remain unchanged.
These reused development recordings do not establish fresh or live accuracy.

Next voice work is reviewed quiet/normal/distant positives, long representative
speech/music/room negatives and fresh held-out sessions; then compare wake
recall, false activations, command capture and STT accuracy before selecting a
new threshold or retraining. Preserve train/validation/test separation and the
external workspace/GPU pause controls. Lowering the threshold and claiming an
accuracy improvement without this comparison would lose the existing evidence.

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
