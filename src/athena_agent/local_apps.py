"""Windows-owned app discovery, launching and closing without cloud inference."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import PureWindowsPath
import re
import time
from uuid import uuid4

from jsonschema.validators import validator_for

from router import validate_action
from .application_catalog import ApplicationCatalog
from .config import ServerConfig, load_server_config
from .tools import MCPPeer, _local_refs_only


class AppNotSent(RuntimeError):
    pass


class AppUnknownOutcome(RuntimeError):
    pass


class AppNeedsWindowChoice(AppNotSent):
    pass


@dataclass(frozen=True)
class AppExecutionResult:
    status: str
    reason: str
    choices: tuple = ()


@dataclass(frozen=True)
class LocalAppsConfig:
    server: ServerConfig
    executable: str = r'C:\Windows\System32\notepad.exe'
    timeout: float = 15
    discovery_timeout: float = 5
    catalog_ttl: float = 60

    def __post_init__(self):
        if not isinstance(self.server, ServerConfig) or self.server.name != 'windows':
            raise ValueError('Local applications require the windows MCP server.')
        if not isinstance(self.executable, str):
            raise ValueError('The Notepad executable must be a configured path.')
        path = PureWindowsPath(self.executable)
        if not path.is_absolute() or path.name.casefold() != 'notepad.exe':
            raise ValueError('Configure an absolute path to the registered Notepad executable.')
        for value in (self.timeout, self.discovery_timeout, self.catalog_ttl):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError('App timeouts must be finite positive values up to 60 seconds.')

    @classmethod
    def from_env(cls):
        return cls(load_server_config('windows'),
                   os.getenv('ATHENA_NOTEPAD_EXECUTABLE', r'C:\Windows\System32\notepad.exe'))


class DirectApps:
    def __init__(self, config, *, peer_factory=MCPPeer, clock=time.monotonic):
        self.config, self.peer_factory = config, peer_factory
        self.clock = clock
        self.snapshot = None
        self._catalog_checked = float('-inf')

    async def catalog(self, *, refresh=False):
        if not refresh and self.clock() - self._catalog_checked < self.config.catalog_ttl:
            return self.snapshot
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            try:
                tools = await peer.start()
                names = [tool.name for tool in tools]
                if 'AthenaAppCatalog' not in names and 'AthenaLaunchApp' not in names:
                    # Compatibility with the previous, fixed-Notepad deployment.
                    self.snapshot, self._catalog_checked = None, self.clock()
                    return None
                _check_tool(tools, 'AthenaAppCatalog', {'refresh': refresh})
                _check_tool(tools, 'AthenaLaunchApp', {'application_id': 'app_' + '0' * 32,
                            'revision': '0' * 64, 'request_id': 'schema_check'})
                result = await peer.call('AthenaAppCatalog', {'refresh': refresh})
                if result.isError:
                    raise ValueError('Catalog tool failed.')
                snapshot = ApplicationCatalog.from_document(_document(result))
                if any('close' in app.operations for app in snapshot.applications.values()):
                    _check_tool(tools, 'AthenaAppWindows', {'application_id': 'app_' + '0' * 32,
                                'revision': snapshot.revision})
                    _check_tool(tools, 'AthenaCloseApp', {'application_id': 'app_' + '0' * 32,
                                'revision': snapshot.revision, 'request_id': 'schema_check'})
            except Exception:
                raise AppNotSent('Windows application catalog is unavailable or incompatible.') from None
            self.snapshot, self._catalog_checked = snapshot, self.clock()
            return snapshot
        finally:
            await peer.close()

    def display_name(self, action):
        if self.snapshot is not None and action.target in self.snapshot.applications:
            return self.snapshot.applications[action.target].display_name
        return 'Notepad' if action.target == 'notepad' else 'Application'

    def _validate(self, action):
        validate_action(action, applications=self.snapshot.vocabulary if self.snapshot else None)
        if (action.backend != 'desktop' or action.target_type != 'application'
                or action.action not in {'launch_app', 'close_app'}):
            raise ValueError('Only registered application actions are supported.')
        operation = 'close' if action.action == 'close_app' else 'launch'
        if operation == 'close' and self.snapshot is None:
            raise AppNotSent('The Windows extension does not support graceful closing.')
        if self.snapshot is not None:
            app = self.snapshot.applications.get(action.target)
            if app is None:
                raise ValueError('The application is absent from the Windows catalog.')
            if operation not in app.operations:
                raise AppNotSent('The requested application operation is unavailable.')

    async def close(self):
        # Every request owns/closes its peer; no long-lived connection is retained.
        pass

    async def preflight(self, action):
        self._validate(action)
        if self.snapshot is not None:
            revision = self.snapshot.revision
            snapshot = await self.catalog(refresh=True)
            if snapshot is None or snapshot.revision != revision:
                raise AppNotSent('Windows application catalog changed. No action sent.')
            self._validate(action)
            if action.action == 'close_app':
                rows = await self._windows(action)
                if len(rows) != 1:
                    if rows:
                        raise AppNeedsWindowChoice('Repeat the close command separately to select a window.')
                    raise AppNotSent('No matching window could be identified.')
            return
        await self._legacy_preflight(action)

    async def _legacy_preflight(self, action):
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

    async def execute_request(self, action, *, request_id, window_id=None):
        return await self.execute(action, request_id=request_id, window_id=window_id)

    async def execute(self, action, *, request_id=None, window_id=None):
        self._validate(action)
        if action.action == 'close_app':
            return await self._close_window(action, request_id=request_id, window_id=window_id)
        if self.snapshot is None:
            return await self._legacy_execute(action)
        request_id = uuid4().hex if request_id is None else request_id
        arguments = {'application_id': action.target, 'revision': self.snapshot.revision,
                     'request_id': request_id}
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            try:
                tools = await peer.start()
                _check_tool(tools, 'AthenaLaunchApp', arguments)
            except Exception:
                raise AppNotSent('Windows launch tool is unavailable or incompatible; no launch sent.') from None
            try:
                result = await peer.call('AthenaLaunchApp', arguments)
            except Exception:
                raise AppUnknownOutcome('Windows launch outcome is unknown; not retried.') from None
            if result.isError:
                raise AppUnknownOutcome('Windows reported an error; launch outcome is unknown.')
            document = _document(result)
            if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
                    or document['schema_version'] != 1 or document.get('window_ready') is not False
                    or any(document.get(key) != value for key, value in arguments.items())):
                raise AppUnknownOutcome('Windows returned an invalid launch receipt; not retried.')
            if document.get('status') == 'not_started':
                # The next new turn fetches a fresh snapshot; this turn never retries.
                self._catalog_checked = float('-inf')
                raise AppNotSent('Windows could not use the selected catalog entry. No launch sent.')
            if document.get('status') != 'accepted':
                raise AppUnknownOutcome('Windows did not confirm the launch handoff; not retried.')
        finally:
            await peer.close()

    async def _windows(self, action):
        arguments = {'application_id': action.target, 'revision': self.snapshot.revision}
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            tools = await peer.start()
            _check_tool(tools, 'AthenaAppWindows', arguments)
            result = await peer.call('AthenaAppWindows', arguments)
            document = _document(result)
            if (result.isError or type(document.get('schema_version')) is not int
                    or document['schema_version'] != 1
                    or document.get('status') != 'ready'
                    or any(document.get(key) != value for key, value in arguments.items())):
                raise AppNotSent('Windows could not identify the application windows.')
            return _choices(document.get('windows'))
        finally:
            await peer.close()

    async def _close_window(self, action, *, request_id=None, window_id=None):
        arguments = {'application_id': action.target, 'revision': self.snapshot.revision,
                     'request_id': uuid4().hex if request_id is None else request_id}
        if window_id is not None:
            if not isinstance(window_id, str) or not re.fullmatch(r'win_[0-9a-f]{32}', window_id):
                raise ValueError('Use an issued window ID.')
            arguments['window_id'] = window_id
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            try:
                tools = await peer.start()
                _check_tool(tools, 'AthenaCloseApp', arguments)
            except Exception:
                raise AppNotSent('Windows close tool is unavailable or incompatible; no close sent.') from None
            try:
                result = await peer.call('AthenaCloseApp', arguments)
            except Exception:
                raise AppUnknownOutcome('Windows close outcome is unknown; not retried.') from None
            document = _document(result)
            if (type(document.get('schema_version')) is not int or document['schema_version'] != 1
                    or document.get('operation') != 'close'
                    or any(document.get(key) != value for key, value in arguments.items())
                    or type(document.get('message_sent')) is not bool
                    or type(document.get('window_closed')) is not bool
                    or not isinstance(document.get('reason'), str)
                    or len(document['reason']) > 80):
                raise AppUnknownOutcome('Windows returned an invalid close receipt; not retried.')
            status = document.get('status')
            rows = _choices(document.get('windows'))
            sent, closed = document['message_sent'], document['window_closed']
            if sent and (not isinstance(document.get('window_id'), str)
                         or not re.fullmatch(r'win_[0-9a-f]{32}', document['window_id'])):
                raise AppUnknownOutcome('Windows did not identify the selected window; not retried.')
            valid = ((status == 'closed' and sent and closed)
                     or (status in {'hidden', 'pending'} and sent and not closed)
                     or (status == 'clarification' and not sent and not closed and len(rows) > 1)
                     or (status == 'not_started' and not sent and not closed))
            if not valid or (result.isError and status in {'closed', 'hidden'}):
                raise AppUnknownOutcome('Windows did not confirm the close outcome; not retried.')
            if document['reason'] == 'catalog_changed':
                self._catalog_checked = float('-inf')
            return AppExecutionResult(status, document['reason'], rows)
        finally:
            await peer.close()

    async def _legacy_execute(self, action):
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


def _check_tool(tools, name, arguments):
    matches = [tool for tool in tools if tool.name == name]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous application tool.')
    schema = matches[0].inputSchema
    _local_refs_only(schema)
    validator = validator_for(schema)
    validator.check_schema(schema)
    if not validator(schema).is_valid(arguments):
        raise ValueError('Incompatible application tool schema.')


def _document(result):
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for block in result.content:
        if block.type == 'text' and len(block.text) <= 1024 * 1024:
            try:
                value = json.loads(block.text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise AppUnknownOutcome('Windows returned no structured application result.')


def _choices(rows):
    if not isinstance(rows, list) or len(rows) > 32:
        raise AppUnknownOutcome('Windows returned invalid window choices.')
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not re.fullmatch(r'win_[0-9a-f]{32}', row['id']) or row['id'] in seen
                or not isinstance(row.get('title'), str) or len(row['title']) > 240
                or any(ord(char) < 32 or ord(char) == 127 for char in row['title'])):
            raise AppUnknownOutcome('Windows returned an invalid window choice.')
        seen.add(row['id'])
        result.append((row['id'], row['title']))
    return tuple(result)
