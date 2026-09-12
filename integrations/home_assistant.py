import os

import requests
from dotenv import load_dotenv

load_dotenv()

HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")

def call_service(domain, service, entity_id):
    url = f"{HA_URL}/api/services/{domain}/{service}"

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    data = {
        "entity_id": entity_id
    }

    response = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=5
    )
    
    response.raise_for_status()
    