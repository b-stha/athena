import asyncio
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from mcp.types import CallToolResult, TextContent, Tool

from athena_agent.config import ServerConfig
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_apps import AppNotSent, AppUnknownOutcome, DirectApps, LocalAppsConfig
from athena_agent.planning import plan_command
from routing_fakes import SimulatedCloud, SimulatedHA
from resolver import AmbiguousTarget, resolve_target
from router import Action


class TargetTests(unittest.TestCase):
    def test_tighter_bounds_keep_close_targets_and_reject_old_broad_matches(self):
        candidates = {'notepad': 'notepad'}
        for name in ['no pad', 'node pad', 'note pad', 'nodpad', 'notepadd']:
            with self.subTest(name=name):
                self.assertEqual(resolve_target(name, candidates), 'notepad')
        for name in ['no path', 'no padded', 'no pass', 'wordpad', 'calculator', 'pad',
                     'notepad2', 'my notepad', 'not pad', 'notepad and shutdown', 'notepad now']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                resolve_target(name, candidates)

    def test_aliases_are_grouped_but_close_distinct_targets_are_ambiguous(self):
        self.assertEqual(resolve_target('bath rum', {'bathroom': 'bathroom', 'bath room': 'bathroom'}), 'bathroom')
        with self.assertRaises(AmbiguousTarget):
            resolve_target('node pad', {'notepad': 'notepad', 'notepod': 'notepod'})
        with self.assertRaises(AmbiguousTarget):
            resolve_target('bedroomm', {'bedroom': 'bedroom', 'bedrooms': 'bedrooms'})

    def test_target_corrections_preserve_original_source_and_do_not_touch_verbs(self):
        for text, backend, action, target in [
            ('Open no pad.', 'desktop', 'launch_app', 'notepad'),
            ('Athena, launch node pad', 'desktop', 'launch_app', 'notepad'),
            ('please launch no pad please', 'desktop', 'launch_app', 'notepad'),
            ('turn off nanoleafes', 'home_assistant', 'turn_off', 'light.nanoleafs'),
            ('turn on bath rum', 'home_assistant', 'turn_on', 'light.bathroom')]:
            with self.subTest(text=text):
                plan = plan_command(text)
                step, = plan.steps
                self.assertEqual(step.reason, 'fuzzy_registered_target')
                self.assertEqual((step.action.backend, step.action.action, step.action.target), (backend, action, target))
                self.assertEqual(plan.original, text)
                self.assertEqual(text[step.start:step.end], step.text)
        for text in ['opn notepad', 'turn of bathroom', 'turn o nbathroom lights', 'wake my pea sea']:
            with self.subTest(text=text):
                self.assertTrue(all(step.action is None for step in plan_command(text).steps))

    def test_ambiguous_fuzzy_target_selects_clarification(self):
        with patch.dict('router.APPLICATIONS', {'notepod': 'notepod'}):
            step, = plan_command('open node pad').steps
            self.assertEqual((step.route, step.reason), ('clarify', 'ambiguous_registered_target'))


SCHEMA = {'type': 'object', 'properties': {'mode': {'enum': ['launch_executable']},
    'executable': {'type': 'string'}, 'args': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['mode', 'executable'], 'additionalProperties': False}


class FakePeer:
    def __init__(self, *args):
        self.calls, self.closed, self.error = [], False, None
        self.tools = [Tool(name='App', inputSchema=SCHEMA)]
        self.document = {'pid': 42, 'executable': r'C:\Windows\System32\notepad.exe', 'args': [], 'cwd': None}

    async def start(self):
        return self.tools

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(self.document))])

    async def close(self):
        self.closed = True


class AppExecutionTests(unittest.IsolatedAsyncioTestCase):
    def executor(self):
        self.peer = FakePeer()
        config = LocalAppsConfig(ServerConfig('windows', 'http://pc.test/mcp', 'private-token'))
        return DirectApps(config, peer_factory=lambda *args: self.peer)

    async def test_fixed_executable_request_uses_only_windows_app_and_no_transcript_arguments(self):
        executor = self.executor()
        await executor.execute(Action('desktop', 'launch_app', 'notepad', 'application'))
        self.assertEqual(self.peer.calls, [('App', {'mode': 'launch_executable',
            'executable': r'C:\Windows\System32\notepad.exe', 'args': []})])
        self.assertTrue(self.peer.closed)

    async def test_missing_or_incompatible_schema_sends_nothing(self):
        executor = self.executor()
        self.peer.tools = [Tool(name='App', inputSchema={'type': 'object', 'properties': {}, 'additionalProperties': False})]
        with self.assertRaises(AppNotSent):
            await executor.execute(Action('desktop', 'launch_app', 'notepad', 'application'))
        self.assertEqual(self.peer.calls, [])
        self.assertTrue(self.peer.closed)

    async def test_timeout_wrong_executable_and_malformed_results_are_not_retried(self):
        for problem in ['timeout', 'wrong_executable', 'invalid_pid']:
            with self.subTest(problem=problem):
                executor = self.executor()
                if problem == 'timeout':
                    self.peer.error = TimeoutError('private transport details')
                elif problem == 'wrong_executable':
                    self.peer.document['executable'] = r'C:\Windows\System32\cmd.exe'
                else:
                    self.peer.document['pid'] = True
                with self.assertRaises(AppUnknownOutcome):
                    await executor.execute(Action('desktop', 'launch_app', 'notepad', 'application'))
                self.assertEqual(len(self.peer.calls), 1)
                self.assertTrue(self.peer.closed)

    async def test_power_actions_and_unregistered_apps_are_rejected_before_connection(self):
        executor = self.executor()
        for action in [Action('desktop', 'shutdown', 'pc', 'device'),
                       Action('desktop', 'launch_app', 'cmd', 'application')]:
            with self.assertRaises(ValueError):
                await executor.execute(action)
        self.assertEqual(self.peer.calls, [])

    def test_windows_transport_configuration_needs_no_model_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent.json'
            path.write_text(json.dumps({'servers': [{'name': 'windows', 'url': 'http://pc.test/mcp', 'token_env': 'WINDOWS_TOKEN'}]}))
            with patch.dict(os.environ, {'ATHENA_AGENT_CONFIG': str(path), 'WINDOWS_TOKEN': 'private-token'}, clear=True):
                config = LocalAppsConfig.from_env()
        self.assertEqual(config.server.name, 'windows')
        self.assertNotIn('private-token', repr(config))


class FuzzyDispatchTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, apps_enabled=True):
        self.apps, self.ha, self.cloud = SimulatedHA(), SimulatedHA(), SimulatedCloud()
        dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=apps_enabled,
            local_factory=lambda: self.ha, apps_factory=lambda: self.apps, agent_factory=lambda: self.cloud,
            event_sink=lambda event: None)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_open_no_pad_and_ha_typos_need_zero_model_requests(self):
        dispatcher = self.make()
        with patch('socket.socket', side_effect=AssertionError('No real network permitted')):
            reply = await dispatcher.turn('open no pad', request_id='app')
            self.assertIn('Notepad launch accepted', reply)
            self.assertEqual(dispatcher.last_route_event.reason, 'fuzzy_registered_application_target')
            self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
            await dispatcher.turn('turn off nanoleafes')
            self.assertEqual(dispatcher.last_route_event.reason, 'fuzzy_registered_ha_target')
        self.assertEqual(self.apps.actions, [Action('desktop', 'launch_app', 'notepad', 'application')])
        self.assertEqual(len(self.ha.actions), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_scope_and_weak_matches_never_launch_a_corrected_app(self):
        cases = ["don't open no pad", 'do not open node pad', 'never launch note pad',
                 'open not pad', 'open no path', 'open no padded', 'open no pass',
                 'open wordpad', 'open calculator', 'open notepad2', 'open my notepad', 'open a notepad',
                 'open no pad and restart', 'open no pad only if I ask',
                 'open no pad before I leave', 'open "no pad"', 'opn no pad',
                 'turn of bathroom lights', 'wake my pea sea', 'open no pad?']
        for text in cases:
            with self.subTest(text=text):
                dispatcher = self.make()
                await dispatcher.turn(text)
                self.assertEqual(self.apps.actions, [])
                self.assertEqual(self.ha.actions, [])
                self.assertEqual(self.cloud.requests, [text])

    async def test_app_launch_failure_and_duplicate_id_never_fall_back_to_model(self):
        dispatcher = self.make()
        async def fail(action):
            self.apps.actions.append(action)
            raise AppUnknownOutcome()
        self.apps.execute = fail
        first = await dispatcher.turn('open no pad', request_id='same')
        second = await dispatcher.turn('open no pad', request_id='same')
        self.assertEqual(first, second)
        self.assertIn('unknown', first)
        self.assertEqual(len(self.apps.actions), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_apps_opt_in_is_independent_of_ha(self):
        dispatcher = self.make(apps_enabled=False)
        await dispatcher.turn('open no pad')
        self.assertEqual(self.apps.actions, [])
        self.assertEqual(self.cloud.requests, ['open no pad'])

    async def test_ambiguous_target_clarifies_without_model(self):
        dispatcher = self.make()
        with patch.dict('router.APPLICATIONS', {'notepod': 'notepod'}):
            reply = await dispatcher.turn('open node pad')
        self.assertEqual(reply, 'Please repeat the full target name.')
        self.assertEqual(self.apps.actions, [])
        self.assertEqual(self.cloud.requests, [])


if __name__ == '__main__':
    unittest.main()
