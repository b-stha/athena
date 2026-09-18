import os
import re
import socket

from dotenv import load_dotenv

load_dotenv()


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
