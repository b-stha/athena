# Athena

Athena is a voice and text assistant running on a Raspberry Pi. It controls
Home Assistant lights, Windows applications, and PC power. Familiar commands run without
a cloud model; less familiar requests can ask a cloud model for help.

## What works today

- Turn registered lights on and off.
- Open apps using the names Windows provides.
- Close app windows normally, leaving save prompts for you to answer.
- Combine up to three supported commands, such as
  `turn on nanoleafs and open Notepad`.
- Wake a configured PC over the network.
- Shut down, restart, or sleep the PC through its Windows tools.
- Use cloud reasoning and, when needed, Windows screenshots for other requests.

Voice replies currently appear as text in the terminal or service logs.
Wake detection and transcription still make mistakes.

## How it works

Speech is turned into text on the Pi. Typed commands enter at the same point.
Athena checks for a known command, then either runs it directly or asks the cloud
model to interpret it.

Both paths use the same Home Assistant and Windows connections. These use
MCP, a standard way to make tools available to an assistant. The Pi chooses
what to call; Athena Desktop keeps the Windows tools running.

## Setup

The Pi needs Python 3.10 or newer, a microphone, the local Whisper speech
service, and access to your Home Assistant and Windows tools.

From the Pi checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-agent.txt -r voice/requirements-wake.txt
.venv/bin/python -m pip install -e '.[agent,wake]'
```

Copy `.env.example` to `.env` and `agent.example.json` to `agent.local.json`.
Add your cloud model settings, connection tokens, and wake-model paths.
Keep these private files out of Git.

Set `ATHENA_LOCAL_HA_ENABLED=true` and `ATHENA_LOCAL_APPS_ENABLED=true`
in `.env` to enable commands that run without the cloud model. The Windows
setting covers both apps and PC power.
See [voice setup](voice/README.md) and [Athena Desktop](https://github.com/b-stha/athena-desktop)
for the remaining setup.

## Use

```bash
./run.sh --text   # type commands
./run.sh          # listen for voice commands
```

The already-configured Pi also has a `~/.local/bin/athena` launcher that loads
its private settings. Its voice listener normally runs as a background service;
stop that listener before starting another voice session.

Try `open Notepad`, then `close Notepad`. If several windows match, Athena lists
them. Reply `close window one` or another listed number within 50 seconds.
Other ways of naming a window, such as "the second Firefox window", are not
currently handled by that local selection step.

A launch being accepted does not mean its window is ready. Closing a window
may hide an app in the tray rather than end it. Athena reports these differences
and keeps recent results so a repeated request ID does not repeat the action.
That history is cleared when the process restarts.

PC power commands include `turn off my computer`, `restart PC`, and
`put my computer to sleep`. Desktop needs the updated power tools installed.
Athena schedules the request briefly before Windows acts; it cannot confirm
that the PC finished shutting down or sleeping. Unsaved work may block shutdown.
In a combined command, put one PC power action last, for example
`turn off nanoleafs then shutdown PC`.

## Still to do

- Split requests that mix known commands with contextual work. Today, a partly
  understood request is sent whole to the model or asks for clarification.
- Save confirmed app nicknames so repeated phrases can run locally.
  Athena does not learn those aliases automatically yet.
- Improve speech accuracy and false wake detections. Voice training is paused.

## Working on Athena

`src/` contains the application, `voice/` contains voice setup and model
information, and `tests/` contains checks. Training and research tools live
outside this repo in the sibling `athena_tools/` folder. Local `docs/` are ignored.

Run the checks with:

```bash
.venv/bin/python -m unittest discover -s tests
```
