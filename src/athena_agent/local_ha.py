"""Registered HA light actions over MCP, with no model or REST fallback."""

from dataclasses import dataclass
import json
import math
import re

from jsonschema.validators import validator_for
import yaml

from router import validate_action
from .config import ServerConfig, load_server_config
from .tools import MCPPeer, _local_refs_only


class HANotSent(RuntimeError):
    """Discovery, schema or target checks failed before the control call."""


class HARejected(RuntimeError):
    """HA explicitly reported an error with no successful actions."""


class HAUnknownOutcome(RuntimeError):
    """A control call may have taken effect; never automatically retry."""


@dataclass(frozen=True)
class LocalHAConfig:
    server: ServerConfig
    timeout: float = 15
    discovery_timeout: float = 5

    def __post_init__(self):
        if not isinstance(self.server, ServerConfig) or self.server.name != 'ha':
            raise ValueError('Local Home Assistant actions require the ha MCP server.')
        for value in (self.timeout, self.discovery_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError('HA timeouts must be finite positive values up to 60 seconds.')

    @classmethod
    def from_env(cls):
        return cls(load_server_config('ha'))


def _tool(tools, names, arguments):
    matches = [tool for tool in tools if tool.name in names]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous HA MCP tool.')
    tool = matches[0]
    schema = tool.inputSchema
    _local_refs_only(schema)
    check = validator_for(schema)
    check.check_schema(schema)
    # Undeclared arguments might be ignored, broadening a named light command.
    if not arguments.keys() <= schema.get('properties', {}).keys() or not check(schema).is_valid(arguments):
        raise ValueError('HA tool does not declare/support the bounded request.')
    return tool.name


def _document(result):
    if result.isError:
        raise ValueError('HA reported an MCP execution error.')
    documents = []
    if isinstance(result.structuredContent, dict):
        documents.append(result.structuredContent)
    for block in result.content:
        if block.type == 'text':
            value = json.loads(block.text)
            if not isinstance(value, dict):
                raise ValueError('Expected an HA result object.')
            documents.append(value)
    if not documents or any(value != documents[0] for value in documents[1:]):
        raise ValueError('Missing or inconsistent HA result.')
    return documents[0]


def _names(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('Invalid HA inventory name/area.')
    names = tuple(dict.fromkeys(part.strip().casefold() for part in value.split(',') if part.strip()))
    if not names:
        raise ValueError('Empty HA inventory names.')
    return names


def _binding(document, action):
    if document.get('success') is not True or not isinstance(document.get('result'), str):
        raise ValueError('HA context was not successful.')
    text = document['result']
    prefix = 'Live Context: An overview of the areas and the devices in this smart home:\n'
    if not text.startswith(prefix) or len(text) > 128_000:
        raise ValueError('Unsupported HA live-context format.')
    rows = yaml.safe_load(text[len(prefix):])
    if not isinstance(rows, list) or not 0 < len(rows) <= 256:
        raise ValueError('Invalid HA light inventory.')
    lights = []
    for row in rows:
        if not isinstance(row, dict) or row.get('domain') != 'light':
            raise ValueError('Unexpected HA inventory domain.')
        aliases = _names(row.get('names'))
        areas = _names(row['areas']) if row.get('areas') else ()
        lights.append((aliases, areas))
    label = action.target.removeprefix('light.').replace('_', ' ').casefold()
    arguments = {'domain': ['light']}
    if action.target_type == 'entity_id':
        selected = [item for item in lights if label in item[0]]
        if len(selected) != 1:
            raise ValueError('Registered HA light is absent or ambiguous.')
        arguments['name'] = label
        if len(selected[0][1]) == 1:
            arguments['area'] = selected[0][1][0]
    else:
        selected = [item for item in lights if label in item[1]]
        if not selected:
            raise ValueError('Registered HA area has no exposed lights.')
        arguments['area'] = label
    aliases = [name for item in selected for name in item[0]]
    if len(aliases) != len(set(aliases)):
        raise ValueError('Ambiguous exposed light names.')
    return arguments, tuple(item[0] for item in selected)


def _receipt(document, action, expected_names, area):
    data = document.get('data')
    if not isinstance(data, dict):
        raise HAUnknownOutcome('Home Assistant result is unknown; not retried.')
    successes, failures = data.get('success'), data.get('failed')
    if document.get('response_type') == 'error' and successes == [] and isinstance(failures, list) and failures:
        raise HARejected('Home Assistant rejected the request; not retried.')
    if (document.get('response_type') != 'action_done' or failures != []
            or not isinstance(successes, list)):
        raise HAUnknownOutcome('Home Assistant did not confirm the complete request; not retried.')
    entities, area_seen = [], False
    for target in successes:
        # Assist includes an area success as scope metadata, then the affected
        # entities. It is not another light and must not widen the requested scope.
        if isinstance(target, dict) and target.get('type') == 'area':
            if (area_seen or area is None or not isinstance(target.get('name'), str)
                    or target['name'].casefold() != area or not isinstance(target.get('id'), str)
                    or not re.fullmatch(r'[a-z0-9_]+', target['id'])
                    or (action.target_type == 'area_id' and target['id'] != action.target)):
                raise HAUnknownOutcome('Home Assistant reported an unexpected area; not retried.')
            area_seen = True
        else:
            entities.append(target)
    if len(entities) != len(expected_names):
        raise HAUnknownOutcome('Home Assistant did not confirm all expected lights; not retried.')
    ids, matched = set(), set()
    for target in entities:
        if (not isinstance(target, dict) or target.get('type') != 'entity'
                or not isinstance(target.get('id'), str) or not re.fullmatch(r'light\.[a-z0-9_]+', target['id'])
                or target['id'] in ids or not isinstance(target.get('name'), str)):
            raise HAUnknownOutcome('Home Assistant returned an invalid target receipt; not retried.')
        ids.add(target['id'])
        candidates = [index for index, names in enumerate(expected_names) if target['name'].casefold() in names]
        if len(candidates) != 1 or candidates[0] in matched:
            raise HAUnknownOutcome('Home Assistant returned unexpected target names; not retried.')
        matched.add(candidates[0])
    if action.target_type == 'entity_id' and ids != {action.target}:
        raise HAUnknownOutcome('Home Assistant reported a different entity; not retried.')
    # Receipt acceptance is not physical state verification.


class HomeAssistantMCP:
    def __init__(self, config, *, peer_factory=MCPPeer):
        self.config, self.peer_factory = config, peer_factory

    async def close(self):
        # Each request owns its MCP peer, including failure/cancellation cleanup.
        pass

    async def preflight(self, action):
        await self._run(action, execute=False)

    async def execute(self, action):
        await self._run(action, execute=True)

    async def _run(self, action, *, execute):
        validate_action(action)
        if action.backend != 'home_assistant':
            raise ValueError('Only registered HA light actions are supported.')
        peer = self.peer_factory(self.config.server, self.config.timeout, self.config.discovery_timeout)
        try:
            try:
                tools = await peer.start()
                context_arguments = {'domain': 'light'}
                context = _tool(tools, {'homeassistant__GetLiveContext', 'GetLiveContext'}, context_arguments)
                arguments, names = _binding(_document(await peer.call(context, context_arguments)), action)
                intent = 'HassTurnOn' if action.action == 'turn_on' else 'HassTurnOff'
                name = _tool(tools, {intent, 'intent__' + intent}, arguments)
            except Exception:
                raise HANotSent('Home Assistant MCP was unavailable, incompatible or ambiguous. No action sent.') from None
            if not execute:
                return
            try:
                result = await peer.call(name, arguments)
                document = _document(result)
            except Exception:
                raise HAUnknownOutcome('Home Assistant outcome is unknown; not retried.') from None
            _receipt(document, action, names, arguments.get('area'))
        finally:
            try:
                await peer.close()
            except Exception:
                # Cleanup failure cannot erase an already validated execution receipt.
                pass
