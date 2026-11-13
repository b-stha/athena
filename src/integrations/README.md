# Integration adapters and historical diagnostics

These paths are retained for `--legacy-text` diagnostics. Normal voice and
`--text` operation use the shared dispatcher with local HA/Windows-MCP execution
and contextual cloud/MCP requests; see
[agent setup](../../docs/AGENT_SETUP.md). The native Wake-on-LAN helper is also
exposed as an agent tool when configured.

## Home Assistant

All HA execution uses [the shared MCP executor](../../docs/HA_MCP.md), including
`--legacy-text`. `home_assistant.call_service()` is only a synchronous adapter for
registered light actions; it does not construct REST requests or accept arbitrary
services. Use the `ha` entry in `agent.local.json` and its named MCP token. The
old `HA_URL`/`HA_TOKEN` settings are unused. Successful diagnostic output denotes
request acceptance, not observed physical state.

Current Athena Desktop no longer exposes the legacy HTTP server or `/commands`.
The Desktop app/power instructions below require a Desktop version from before
that backend was removed. The retained Pi diagnostics do not bridge HTTP to MCP.
Normal voice/text Windows-MCP operation and Pi-native Wake-on-LAN are unaffected.

## Historical Desktop HTTP connection

Set `DESKTOP_URL` in Athena's `.env` to the desktop server's base address:

```dotenv
DESKTOP_URL=http://YOUR_PC_LAN_IP:5000
```

The desktop server must listen on that LAN address using `ATHENA_BIND_HOST`,
or a complete URL override in `ATHENA_HTTP_PREFIX` (including a trailing slash).
Its default localhost binding cannot accept requests from the Pi.
That historical HTTP server has no authentication, so use a trusted development
network.

Run `.venv/bin/python main.py --legacy-text` and enter `open notepad`.
The normal voice path uses MCP instead of this legacy REST interface. Athena sends `POST /commands` with:

```json
{
  "requestId": "<generated UUID>",
  "command": "launch_app",
  "parameters": { "name": "notepad" }
}
```

The request has a five-second timeout and is not automatically retried.
A successful response must contain the matching `requestId` and `success: true`.
HTTP errors, failed commands, and mismatched responses do not report success.

Earlier versions of the [Athena Desktop](https://github.com/b-stha/athena-desktop)
C#/.NET agent implemented `launch_app`, mapping `notepad` to its Windows executable.
Those HTTP handlers are absent from current Desktop. In that older backend,
unsupported commands and application names return a failed command result.
Existing Home Assistant light commands continue to use their own integration.

# Wake-on-LAN

Wake-on-LAN is handled inside `desktop.py`, alongside desktop HTTP requests.
Both PC wake phrases use the `desktop` backend; `execute()` selects the transport.
The current agent also calls `wake_pc()` directly through its configured
`native__wake_pc` tool. This UDP implementation is independent of the retired
Desktop HTTP backend and Windows-MCP.

Set `PC_MAC_ADDRESS` in `.env` to the PC's network adapter MAC address
(colon-separated, hyphen-separated, or 12 hexadecimal digits).
Optionally set `WOL_BROADCAST_ADDRESS` to your subnet's broadcast address;
the default is `255.255.255.255`. Athena sends one UDP magic packet to port 9.

Use `turn on PC` or `turn on my PC` in legacy text mode. This does not use
HTTP or require the desktop process to be running. The PC must already be
configured to support Wake-on-LAN. Sending a packet does not confirm startup.

For a live test, configure the MAC address, put the PC in a wake-capable state,
then issue either command from Athena and observe the PC. No automatic retries
or startup detection are implemented. Legacy-text shutdown/restart/sleep requests
use the historical HTTP path described below.

Packet format reference: [AMD Magic Packet Technology](https://www.amd.com/content/dam/amd/en/documents/archived-tech-docs/white-papers/20213.pdf).

# Historical HTTP power commands

Core accepts these commands in legacy text mode, but execution requires an older
Desktop that still exposes the HTTP power handlers:

| Action | Phrases |
| --- | --- |
| Shutdown | `shutdown pc`, `shut down pc`, `turn off pc`, also with `my pc` |
| Restart | `restart pc`, `restart my pc` |
| Sleep | `sleep pc`, `sleep my pc` |

Each sends `POST /commands` with the respective `command` value (`shutdown`,
`restart`, or `sleep`), an empty `parameters` object, and a generated request ID.
The desktop client must register the corresponding power-command handlers.
Core reports acceptance only when the matching response contains `success: true`.
It does not wake an unreachable PC or retry a failed request. Acknowledgment
confirms acceptance of the request, not completion of the Windows power action.

To test against a power-capable client, run `./run.sh --legacy-text` and enter one of the
commands above. These request real PC power actions; save your desktop work first.
