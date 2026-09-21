import os
import re
import socket
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


def execute(action, target):
    if action == "wake":
        wake_pc()
    elif action == "shutdown":
        return send_command("shutdown", {})
    else:
        return send_command(action, {"name": target})


def wake_pc():
    mac = os.getenv("PC_MAC_ADDRESS", "").strip()
    if not re.fullmatch(r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})", mac):
        raise ValueError("Set PC_MAC_ADDRESS in .env to the PC network adapter's MAC address.")

    address = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + address * 16
    broadcast = os.getenv("WOL_BROADCAST_ADDRESS", "255.255.255.255").strip()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sender.settimeout(5)
            sender.sendto(packet, (broadcast, 9))
    except OSError as error:
        raise ValueError("Could not send the Wake-on-LAN packet. Check the network and broadcast address.") from error
