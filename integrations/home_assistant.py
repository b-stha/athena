import os

import requests
from dotenv import load_dotenv

load_dotenv()

HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")

def call_service(domain, service, entity_id=None, *, area_id=None):
    if bool(entity_id) == bool(area_id):
        raise ValueError("Provide exactly one entity_id or area_id.")
    url = f"{HA_URL}/api/services/{domain}/{service}"

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    data = {"entity_id": entity_id} if entity_id else {"area_id": area_id}

    response = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=5
    )

    response.raise_for_status()

