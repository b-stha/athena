"""Direct, single-attempt HA light requests, independent of cloud configuration."""

from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

import httpx

from router import validate_action


class HARejected(RuntimeError):
    """HA returned an explicit HTTP rejection. Never retry through MCP."""


class HAUnknownOutcome(RuntimeError):
    """The request may have taken effect. Never automatically retry."""


@dataclass(frozen=True)
class LocalHAConfig:
    url: str
    token: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.url, str) or not isinstance(self.token, str):
            raise ValueError('HA_URL and HA_TOKEN must be text.')
        parsed = urlsplit(self.url)
        try:
            parsed.port
        except ValueError:
            raise ValueError('HA_URL has an invalid port.') from None
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}
                or any(character.isspace() for character in self.url)):
            raise ValueError('HA_URL must be an HTTP(S) origin without credentials or an API path.')
        if not self.token.strip() or any(character in self.token for character in '\r\n'):
            raise ValueError('Set HA_TOKEN for direct Home Assistant requests.')

    @classmethod
    def from_env(cls):
        return cls(os.getenv('HA_URL', '').strip(), os.getenv('HA_TOKEN', '').strip())


class DirectHA:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.client = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False,
                                        transport=transport)

    async def close(self):
        await self.client.aclose()

    async def execute(self, action):
        validate_action(action)
        if action.backend != 'home_assistant':
            raise ValueError('The local HA executor accepts registered light actions only.')
        try:
            response = await self.client.post(
                f'{self.config.url.rstrip("/")}/api/services/light/{action.action}',
                headers={'Authorization': f'Bearer {self.config.token}'},
                json={action.target_type: action.target})
        except httpx.HTTPError:
            raise HAUnknownOutcome('Home Assistant outcome is unknown; request was not retried.') from None
        if response.status_code in {400, 401, 403, 404, 405, 422, 429}:
            raise HARejected('Home Assistant rejected the request; request was not retried.')
        if response.status_code != 200:
            raise HAUnknownOutcome('Home Assistant outcome is unknown; request was not retried.')
        try:
            body = response.json()
        except ValueError:
            raise HAUnknownOutcome('Home Assistant returned an invalid result; outcome is unknown.') from None
        if not isinstance(body, list):
            raise HAUnknownOutcome('Home Assistant returned an invalid result; outcome is unknown.')
        # A successful service response is acceptance, not physical verification.
