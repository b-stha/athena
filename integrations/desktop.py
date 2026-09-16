import os
from uuid import uuid4

import requests
from dotenv import load_dotenv

load_dotenv()


def send_command(command, parameters):
    base_url = os.getenv("DESKTOP_URL", "").strip()
    if not base_url:
        raise ValueError("Set DESKTOP_URL to the desktop server's base URL in .env.")

    request_id = str(uuid4())
    response = requests.post(
        f"{base_url.rstrip('/')}/commands",
        json={"requestId": request_id, "command": command, "parameters": parameters},
        timeout=5,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get("requestId") != request_id:
        raise ValueError("Desktop returned an invalid or mismatched response.")
    if result.get("success") is not True:
        raise ValueError(result.get("message") or "Desktop command failed.")
    return result
