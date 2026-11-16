# Integration adapters

Normal voice/text requests use the shared dispatcher and MCP executors.
These small adapters remain for explicit `--legacy-text` diagnostics and
Pi-native Wake-on-LAN.

- `home_assistant.py` wraps the same HA MCP executor synchronously. It accepts
  registered light actions using the `ha` server in `agent.local.json`;
  `HA_URL` and `HA_TOKEN` are obsolete.
- `desktop.py` supplies `wake_pc()` to the agent. Set `PC_MAC_ADDRESS` and,
  optionally, `WOL_BROADCAST_ADDRESS` in `.env`. It sends one UDP magic packet;
  acceptance does not confirm the PC has started.
- Desktop HTTP requests are retained only for compatibility diagnostics against
  old Desktop versions. Current Athena Desktop exposes MCP and cannot serve
  those HTTP requests. Normal Windows control does not use this path.

See the [core guide](../../readme.md) and [configuration example](../../agent.example.json).
Historical HTTP setup instructions are archived outside core under
`athena_tools/history/`.
