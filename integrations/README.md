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

The desktop server currently returns HTTP 501 until a command handler is implemented.
This step adds core-side routing and transport only; it cannot launch Notepad yet.
Existing Home Assistant light commands continue to use their own integration.
