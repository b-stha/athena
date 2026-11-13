"""Allowlisted Notepad launch through Windows MCP without cloud inference."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import PureWindowsPath

from jsonschema.validators import validator_for

from router import validate_action
from .config import ServerConfig, load_server_config
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
        return cls(load_server_config('windows'),
                   os.getenv('ATHENA_NOTEPAD_EXECUTABLE', r'C:\Windows\System32\notepad.exe'))


class DirectApps:
    def __init__(self, config, *, peer_factory=MCPPeer):
        self.config, self.peer_factory = config, peer_factory

    async def close(self):
        # Every request owns/closes its peer; no long-lived connection is retained.
        pass

    async def preflight(self, action):
        validate_action(action)
        if (action.backend, action.action, action.target_type, action.target) != (
                'desktop', 'launch_app', 'application', 'notepad'):
            raise ValueError('Only registered Notepad launch is supported.')
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            tools = await peer.start()
            apps = [tool for tool in tools if tool.name == 'App']
            if len(apps) != 1:
                raise ValueError('Missing or ambiguous App tool.')
            schema = apps[0].inputSchema
            _local_refs_only(schema)
            validator = validator_for(schema)
            validator.check_schema(schema)
            if not validator(schema).is_valid({'mode': 'launch_executable',
                    'executable': self.config.executable, 'args': []}):
                raise ValueError('Incompatible App schema.')
        finally:
            await peer.close()

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
