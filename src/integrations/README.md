# Integration helpers

Normal voice and text commands use Athena's shared Home Assistant and Windows
tools. This folder contains the small helpers for waking the PC and running
the older text-only diagnostics.

- `desktop.py` sends a Wake-on-LAN packet when asked to wake the PC.
  Configure `PC_MAC_ADDRESS` and, if needed, `WOL_BROADCAST_ADDRESS` in `.env`.
  Sending the packet does not confirm that the PC has started.
- `home_assistant.py` sends supported light commands through the same
  Home Assistant MCP connection used by normal commands. Configure the `ha`
  connection in `agent.local.json`.

The remaining Desktop HTTP calls are for testing old Desktop versions through
`--legacy-text`. Current Athena Desktop uses Windows-MCP and does not serve
those old requests. The old `HA_URL` and `HA_TOKEN` settings are no longer used.

See the [main guide](../../readme.md) and
[connection example](../../agent.example.json) for current setup.
