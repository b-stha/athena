import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp.types import CallToolResult, Tool

from athena_agent.config import ServerConfig, load_server_config
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_apps import LocalAppsConfig
from athena_agent.local_ha import HomeAssistantMCP, LocalHAConfig, HANotSent, HARejected, HAUnknownOutcome
from router import Action, parse_action
from ha_mcp_fixture import LIGHTS, Peer, context, receipt, result, serve


class HAMCPTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, peer):
        return HomeAssistantMCP(LocalHAConfig(ServerConfig('ha', 'http://ha.test/mcp', 'test-token')),
                                peer_factory=lambda *args: peer)

    async def test_registered_entities_and_area_use_bounded_tools_and_matching_receipts(self):
        for state in ('on', 'off'):
            for row in LIGHTS:
                name = row['names']
                peer = Peer(receipt((name,), area=row['areas']))
                await self.backend(peer).execute(parse_action(f'turn {state} {name}'))
                self.assertEqual(peer.effects, [('intent__HassTurn' + state.title(), {
                    'name': name.casefold(), 'area': row['areas'].casefold(), 'domain': ['light']})])
                self.assertTrue(peer.closed)
            peer = Peer(receipt(('Nanoleafs', 'Table glow', 'Under glow'), area='Bedroom'))
            await self.backend(peer).execute(parse_action(f'turn {state} bedroom'))
            self.assertEqual(peer.effects[0][1], {'area': 'bedroom', 'domain': ['light']})

    async def test_preflight_discovers_and_reads_context_without_effect(self):
        peer = Peer()
        await self.backend(peer).preflight(parse_action('turn on nanoleafs'))
        self.assertEqual(peer.calls, [('homeassistant__GetLiveContext', {'domain': 'light'})])
        self.assertTrue(peer.closed)

    async def test_missing_ambiguous_or_malformed_inventory_sends_no_effect(self):
        for value in [context([]), context(LIGHTS + [LIGHTS[1]]),
                      context([{'names': 'Nanoleafs', 'domain': 'switch'}]),
                      {'success': False, 'result': 'not available'}, {'success': True, 'result': 'changed format'},
                      context([{'names': 'Table glow', 'domain': 'light', 'areas': 'Bedroom'}])]:
            peer = Peer()
            peer.context = value
            with self.subTest(value=value), self.assertRaises(HANotSent):
                await self.backend(peer).execute(parse_action('turn on nanoleafs'))
            self.assertEqual(peer.effects, [])
            self.assertTrue(peer.closed)

    async def test_schema_changes_and_duplicate_tool_bindings_fail_before_control(self):
        variants = []
        base = Peer().tools
        variants.append(base[:1])
        variants.append(base + [Tool(name='HassTurnOn', inputSchema=base[1].inputSchema)])
        for schema in [{'type': 'object'}, {'$ref': 'https://external.invalid/schema'},
                       {'type': 'object', 'properties': {'name': {'type': 'string'}}},
                       {'type': 'object', 'properties': base[1].inputSchema['properties'], 'required': ['other']}]:
            variants.append([base[0], Tool(name=base[1].name, inputSchema=schema), base[2]])
        for tools in variants:
            peer = Peer()
            peer.tools = tools
            with self.subTest(tools=tools), self.assertRaises(HANotSent):
                await self.backend(peer).execute(parse_action('turn on nanoleafs'))
            self.assertEqual(peer.effects, [])
            self.assertTrue(peer.closed)

    async def test_unregistered_or_non_ha_action_never_opens_peer(self):
        factory = Mock(side_effect=AssertionError('No connection permitted'))
        backend = self.backend(Peer())
        backend.peer_factory = factory
        for action in [Action('home_assistant', 'turn_on', 'light.unknown'), parse_action('open Notepad')]:
            with self.assertRaises(ValueError):
                await backend.execute(action)
        factory.assert_not_called()

    async def test_receipt_errors_partial_effects_and_wrong_entities_are_unknown(self):
        wrong_id = receipt()
        wrong_id['data']['success'][0]['id'] = 'light.other'
        partial = receipt()
        partial['data']['failed'] = [{'name': 'other'}]
        duplicate = receipt(('Nanoleafs', 'Nanoleafs'))
        for document in [{}, {'success': True}, receipt(()), wrong_id, partial, duplicate]:
            peer = Peer(document)
            with self.subTest(document=document), self.assertRaises(HAUnknownOutcome):
                await self.backend(peer).execute(parse_action('turn on nanoleafs'))
            self.assertEqual(len(peer.effects), 1)
            self.assertTrue(peer.closed)

    async def test_mcp_error_and_transport_loss_are_unknown_without_retry(self):
        for error in (TimeoutError('private token detail'), OSError('private token detail'), None):
            peer = Peer()
            peer.effect_error = error
            if error is None:
                peer.result = result(receipt(), error=True)
            with self.assertRaises(HAUnknownOutcome) as caught:
                await self.backend(peer).execute(parse_action('turn on nanoleafs'))
            self.assertNotIn('private token', str(caught.exception))
            self.assertEqual(len(peer.effects), 1)

    async def test_explicit_no_success_error_is_rejected_and_partial_error_is_unknown(self):
        peer = Peer({'response_type': 'error', 'data': {'success': [], 'failed': [{'name': 'Nanoleafs'}]}})
        with self.assertRaises(HARejected):
            await self.backend(peer).execute(parse_action('turn on nanoleafs'))
        peer.result = result({'response_type': 'error', 'data': receipt()['data']})
        with self.assertRaises(HAUnknownOutcome):
            await self.backend(peer).execute(parse_action('turn on nanoleafs'))

    async def test_discovery_and_context_failures_never_call_control(self):
        for slot in ('start_error', 'read_error'):
            peer = Peer()
            setattr(peer, slot, OSError('private token detail'))
            with self.assertRaises(HANotSent) as caught:
                await self.backend(peer).execute(parse_action('turn on nanoleafs'))
            self.assertNotIn('private token', str(caught.exception))
            self.assertEqual(peer.effects, [])
            self.assertTrue(peer.closed)

    async def test_close_failure_does_not_erase_accepted_receipt(self):
        peer = Peer()
        peer.close_error = OSError('cleanup only')
        await self.backend(peer).execute(parse_action('turn on nanoleafs'))

    async def test_structured_receipts_work_but_conflicting_text_is_unknown(self):
        peer = Peer()
        peer.result = CallToolResult(content=[], structuredContent=receipt())
        await self.backend(peer).execute(parse_action('turn on nanoleafs'))
        peer.result = result(receipt(('Table glow',)))
        peer.result.structuredContent = receipt()
        with self.assertRaises(HAUnknownOutcome):
            await self.backend(peer).execute(parse_action('turn on nanoleafs'))

    async def test_case_only_alias_duplicates_do_not_create_ambiguity(self):
        peer = Peer()
        rows = [dict(row) for row in LIGHTS]
        rows[1]['names'] = 'Nanoleafs, nanoleafs'
        peer.context = context(rows)
        await self.backend(peer).execute(parse_action('turn on nanoleafs'))
        self.assertEqual(len(peer.effects), 1)

    async def test_unrequested_or_repeated_area_receipts_are_unknown(self):
        for area in ('Bathroom', 'Bedroom'):
            document = receipt(area=area)
            if area == 'Bedroom':
                document['data']['success'].append(document['data']['success'][0])
            with self.subTest(area=area), self.assertRaises(HAUnknownOutcome):
                await self.backend(Peer(document)).execute(parse_action('turn on nanoleafs'))
        document = receipt(('Nanoleafs', 'Table glow', 'Under glow'), area='Bedroom')
        document['data']['success'][0]['id'] = 'different_area'
        with self.assertRaises(HAUnknownOutcome):
            await self.backend(Peer(document)).execute(parse_action('turn on bedroom'))

    async def test_compound_stops_after_ha_receipt_mismatch_without_model_or_windows_effect(self):
        peer = Peer(receipt(('Table glow',)))
        windows = Mock(spec=['preflight', 'execute', 'close'])
        cloud = Mock(side_effect=AssertionError('No cloud permitted'))
        dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=True, agent_factory=cloud,
            local_factory=lambda: self.backend(peer), apps_factory=lambda: windows, event_sink=lambda _: None)
        windows.preflight = AsyncMock()
        windows.close = AsyncMock()
        self.addAsyncCleanup(dispatcher.close)
        await dispatcher.turn('turn on nanoleafs and open Notepad', request_id='partial')
        self.assertEqual([row.status for row in dispatcher.ledger[('default', 'partial')].step_records],
                         ['unknown', 'not_started'])
        windows.execute.assert_not_called()
        self.assertEqual(len(peer.effects), 1)
        cloud.assert_not_called()


class HAConfigTests(unittest.TestCase):
    def test_transport_settings_require_no_model_or_other_server_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent.json'
            settings = {'servers': [{'name': 'ha', 'url': 'http://ha.test/mcp', 'token_env': 'HA_TEST_TOKEN'},
                                    {'name': 'windows', 'url': 'http://pc.test/mcp', 'token_env': 'PC_TEST_TOKEN'}]}
            path.write_text(json.dumps(settings), encoding='utf8')
            with patch.dict(os.environ, {'ATHENA_AGENT_CONFIG': str(path), 'HA_TEST_TOKEN': 'private-token'}, clear=True):
                config = LocalHAConfig.from_env()
                self.assertEqual(config.server.name, 'ha')
                self.assertNotIn('private-token', repr(config))
                self.assertNotIn('HA_URL', os.environ)
                with self.assertRaises(ValueError):
                    LocalAppsConfig.from_env()
                settings['servers'].append(settings['servers'][0])
                path.write_text(json.dumps(settings), encoding='utf8')
                with self.assertRaises(ValueError):
                    load_server_config('ha')

    def test_invalid_server_and_timeouts_are_rejected(self):
        server = ServerConfig('ha', 'http://ha.test/mcp', 'token')
        for value in (True, 0, -1, math.inf, math.nan, 61, '5'):
            with self.assertRaises(ValueError):
                LocalHAConfig(server, timeout=value)
        with self.assertRaises(ValueError):
            LocalHAConfig(ServerConfig('windows', 'http://pc.test/mcp', 'token'))

    def test_sync_diagnostic_uses_same_mcp_executor_and_validates_before_config(self):
        from integrations.home_assistant import call_service
        backend = Mock(execute=AsyncMock(), close=AsyncMock())
        with patch('athena_agent.local_ha.HomeAssistantMCP', return_value=backend), \
                patch('athena_agent.local_ha.LocalHAConfig.from_env') as config:
            call_service('light', 'turn_on', 'light.nanoleafs')
            backend.execute.assert_awaited_once_with(parse_action('turn on nanoleafs'))
            backend.close.assert_awaited_once()
            config.assert_called_once()
            for args in [('switch', 'turn_on', 'light.nanoleafs'), ('light', 'turn_on', 'light.unknown')]:
                with self.assertRaises(ValueError):
                    call_service(*args)
            config.assert_called_once()


class HAWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_transport_receipts_auth_and_id_replay_without_cloud(self):
        for document, disconnect, auth_status, expected in [
            (None, False, 200, 'accepted'), ({}, False, 200, 'unknown'),
            (None, True, 200, 'unknown'), (None, False, 401, 'not_started')]:
            with serve(document=document, disconnect=disconnect, auth_status=auth_status) as (url, calls, auth):
                cloud = Mock(side_effect=AssertionError('No model allowed'))
                config = LocalHAConfig(ServerConfig('ha', url, 'fake-token'), timeout=1)
                dispatcher = RequestDispatcher(local_enabled=True, agent_factory=cloud, event_sink=lambda _:None,
                    local_factory=lambda: HomeAssistantMCP(config))
                try:
                    first = await dispatcher.turn('turn on nanoleafs', request_id='wire')
                    self.assertEqual(first, await dispatcher.turn('turn on nanoleafs', request_id='wire'))
                    self.assertEqual(dispatcher.ledger[('default', 'wire')].status, expected)
                    effects = [call for call in calls if not call[0].endswith('GetLiveContext')]
                    self.assertEqual(len(effects), 0 if auth_status != 200 else 1)
                    self.assertTrue(auth)
                    self.assertEqual(set(auth), {'Bearer fake-token'})
                    self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
                    cloud.assert_not_called()
                finally:
                    await dispatcher.close()


if __name__ == '__main__':
    unittest.main()
