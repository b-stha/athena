# Desktop connection

Set `DESKTOP_URL` in Athena's `.env` to the desktop server's base address:

```dotenv
DESKTOP_URL=http://YOUR_PC_LAN_IP:5000
```

The desktop server must listen on that LAN address using `ATHENA_HTTP_PREFIX`;
its default localhost binding cannot accept requests from the Pi.
The current server has no authentication, so use a trusted development network.

Run `.venv/bin/python main.py --text` and enter `open notepad`.
Voice input can use the same command. Athena sends `POST /commands` with:

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

The [Athena Desktop](https://github.com/b-stha/athena-desktop) C#/.NET client
now supports launching Notepad through `launch_app`. The full voice-to-desktop
Notepad launch has been confirmed in live testing.
Existing Home Assistant light commands continue to use their own integration.

# Wake-on-LAN

Wake-on-LAN is handled inside `desktop.py`, alongside desktop HTTP requests.
Both PC wake phrases use the `desktop` backend; `execute()` selects the transport.

Set `PC_MAC_ADDRESS` in `.env` to the PC's network adapter MAC address
(colon-separated, hyphen-separated, or 12 hexadecimal digits).
Optionally set `WOL_BROADCAST_ADDRESS` to your subnet's broadcast address;
the default is `255.255.255.255`. Athena sends one UDP magic packet to port 9.

Use `turn on PC` or `turn on my PC` in text or voice mode. This does not use
HTTP or require the desktop process to be running. The PC must already be
configured to support Wake-on-LAN. Sending a packet does not confirm startup.

For a live test, configure the MAC address, put the PC in a wake-capable state,
then issue either command from Athena and observe the PC. No automatic retries
or startup detection are implemented. Shutdown requests use the separate HTTP path
described below.

Packet format reference: [AMD Magic Packet Technology](https://www.amd.com/content/dam/amd/en/documents/archived-tech-docs/white-papers/20213.pdf).

# Shutdown command

Core accepts `shutdown PC`, `shut down PC`, and `turn off PC`, also with `my PC`.
These send `POST /commands` with `command: "shutdown"`, an empty `parameters`
object, and a generated request ID. The desktop client must implement this command.
Core reports acceptance only when the matching response contains `success: true`.
It does not wake an unreachable PC or retry a failed request. Acknowledgment is
not proof that Windows finished shutting down.

To test against a shutdown-capable client, run `./run.sh --text` and enter
`shutdown pc`. This requests real PC shutdown; save your desktop work first.
