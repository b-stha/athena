"""Allowlisted Notepad launch through Windows MCP without cloud inference."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PureWindowsPath

from jsonschema.validators import validator_for

from router import validate_action
from .config import ServerConfig
from .tools import MCPPeer, _local_refs_only


class AppNotSent(RuntimeError):
    pass


class AppUnknownOutcome(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalAppsConfig:
    server: ServerConfig
    executable: str = r'C:\Windows\System32\notepad.exe'
    timeout: float = 15
    discovery_timeout: float = 5

    def __post_init__(self):
        if not isinstance(self.server, ServerConfig) or self.server.name != 'windows':
            raise ValueError('Local applications require the windows MCP server.')
        if not isinstance(self.executable, str):
            raise ValueError('The Notepad executable must be a configured path.')
        path = PureWindowsPath(self.executable)
        if not path.is_absolute() or path.name.casefold() != 'notepad.exe':
            raise ValueError('Configure an absolute path to the registered Notepad executable.')
        for value in (self.timeout, self.discovery_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError('App timeouts must be finite positive values up to 60 seconds.')

    @classmethod
    def from_env(cls):
        # Read only Windows transport settings. No model configuration is needed.
        path = Path(os.getenv('ATHENA_AGENT_CONFIG', 'agent.local.json'))
        settings = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(settings, dict):
            raise ValueError('Application configuration must be a JSON object.')
        servers = settings.get('servers', [])
        if not isinstance(servers, list):
            raise ValueError('Application servers must be a list.')
        matches = [item for item in servers if isinstance(item, dict) and item.get('name') == 'windows']
        if len(matches) != 1 or set(matches[0]) != {'name', 'url', 'token_env'}:
            raise ValueError('Configure one windows MCP server for local application launch.')
        item = matches[0]
        if not isinstance(item['token_env'], str):
            raise ValueError('Windows token_env must name an environment variable.')
        server = ServerConfig('windows', item['url'], os.getenv(item['token_env'], ''))
        return cls(server, os.getenv('ATHENA_NOTEPAD_EXECUTABLE', r'C:\Windows\System32\notepad.exe'))


class DirectApps:
    def __init__(self, config, *, peer_factory=MCPPeer):
        self.config, self.peer_factory = config, peer_factory

    async def close(self):
        # Every request owns/closes its peer; no long-lived connection is retained.
        pass

    async def execute(self, action):
        validate_action(action)
        if (action.backend, action.action, action.target_type, action.target) != (
                'desktop', 'launch_app', 'application', 'notepad'):
            raise ValueError('Only the registered Notepad launch is supported.')
        arguments = {'mode': 'launch_executable', 'executable': self.config.executable, 'args': []}
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            try:
                tools = await peer.start()
                apps = [tool for tool in tools if tool.name == 'App']
                if len(apps) != 1:
                    raise ValueError('Missing or ambiguous App tool.')
                schema = apps[0].inputSchema
                _local_refs_only(schema)
                validator = validator_for(schema)
                validator.check_schema(schema)
                if not validator(schema).is_valid(arguments):
                    raise ValueError('Installed App schema does not support the fixed launch request.')
            except Exception:
                raise AppNotSent('Windows MCP was unavailable or incompatible; no launch sent.') from None
            try:
                result = await peer.call('App', arguments)
            except Exception:
                raise AppUnknownOutcome('Windows launch outcome is unknown; not retried.') from None
            if result.isError:
                raise AppUnknownOutcome('Windows reported an error; launch completion is unknown.')
            documents = [result.structuredContent] if isinstance(result.structuredContent, dict) else []
            for block in result.content:
                if block.type == 'text':
                    try:
                        documents.append(json.loads(block.text))
                    except ValueError:
                        pass
            for document in documents:
                if not isinstance(document, dict):
                    continue
                if (type(document.get('pid')) is int and document['pid'] > 0
                        and isinstance(document.get('executable'), str)
                        and PureWindowsPath(document['executable']) == PureWindowsPath(self.config.executable)
                        and document.get('args') == [] and document.get('cwd') is None):
                    return
            raise AppUnknownOutcome('Windows did not confirm the fixed executable launch; not retried.')
        finally:
            await peer.close()
