from copy import deepcopy
from dataclasses import asdict
import json
import unittest
from unittest.mock import Mock

from mcp.types import CallToolResult, TextContent, Tool
from router import Action
from athena_agent.config import ServerConfig
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_power import DirectPower, PowerNotSent, PowerRejected, PowerUnknownOutcome
from athena_agent.planning import plan_command
from athena_agent.routing import route_reason
from routing_fakes import SimulatedCloud, SimulatedHA

STATUS_SCHEMA = {'type': 'object', 'properties': {
    'request_id': {'type': ['string', 'null']}}, 'additionalProperties': False}
ACTION_SCHEMA = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['shutdown', 'restart', 'sleep']},
    'request_id': {'type': 'string'}}, 'required': ['action', 'request_id'], 'additionalProperties': False}


class Endpoint:
    def __init__(self):
        self.calls = []
        self.status = 'scheduled'
        self.error = False
        self.invalid = False
        self.tools = [Tool(name='AthenaPowerStatus', inputSchema=STATUS_SCHEMA),
                      Tool(name='AthenaPowerAction', inputSchema=ACTION_SCHEMA)]
        self.actions = ['shutdown', 'restart', 'sleep']

    def peer(self, *args):
        endpoint = self
        class Peer:
            async def start(self):
                return endpoint.tools
            async def close(self):
                pass
            async def call(self, name, arguments):
                endpoint.calls.append((name, deepcopy(arguments)))
                if name == 'AthenaPowerStatus':
                    document = {'schema_version': 1, 'status': 'ready', 'actions': endpoint.actions,
                                'delay_seconds': 2, 'state_confirmed': False}
                    is_error = False
                else:
                    if endpoint.error:
                        raise TimeoutError()
                    document = {'schema_version': 1, **arguments, 'operation': 'power',
                                'status': endpoint.status, 'reason': 'fixture',
                                'delay_seconds': 2, 'state_confirmed': endpoint.invalid}
                    is_error = endpoint.status not in {'scheduled', 'submitted'}
                return CallToolResult(content=[TextContent(type='text', text=json.dumps(document))],
                                      isError=is_error)
        return Peer()


class PowerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.endpoint = Endpoint()
        self.waker = Mock()
        self.config = Mock()
        self.backend = DirectPower(ServerConfig('windows', 'http://pc.test/mcp', 'private-token'),
            peer_factory=self.endpoint.peer, waker=self.waker, wake_config=self.config)
        self.cloud, self.ha = SimulatedCloud(), SimulatedHA()
        dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=True,
            power_factory=lambda: self.backend, local_factory=lambda: self.ha,
            agent_factory=lambda: self.cloud, event_sink=lambda _: None)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_shutdown_restart_and_sleep_do_not_initialize_a_model(self):
        for text in ['turn off my computer', 'restart PC', 'put my computer to sleep']:
            dispatcher = self.make()
            reply = await dispatcher.turn(text, request_id='power')
            self.assertIn('scheduled in 2 seconds', reply)
            self.assertNotIn('is off', reply)
            self.assertEqual(dispatcher.last_route_event.route, 'local')
            self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
            self.assertEqual(self.cloud.requests, [])
            self.assertEqual([name for name, _ in self.endpoint.calls],
                             ['AthenaPowerStatus', 'AthenaPowerAction'])
            before = deepcopy(self.endpoint.calls)
            self.assertEqual(await dispatcher.turn(text, request_id='power'), reply)
            self.assertEqual(self.endpoint.calls, before)

    async def test_wake_on_lan_needs_no_windows_connection_or_model(self):
        dispatcher = self.make()
        self.backend.server = None
        reply = await dispatcher.turn('wake my pc', request_id='wake')
        self.assertEqual(reply, 'Wake packet sent. PC startup is not confirmed.')
        self.waker.assert_called_once_with()
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.cloud.requests, [])
        await dispatcher.turn('wake my pc', request_id='wake')
        self.waker.assert_called_once_with()

    async def test_failure_or_unknown_never_falls_back_or_retries(self):
        for problem, expected in [('not_started', 'not_started'), ('rejected', 'rejected'),
                                  ('unknown', 'unknown'), ('timeout', 'unknown'), ('malformed', 'unknown')]:
            dispatcher = self.make()
            if problem == 'timeout':
                self.endpoint.error = True
            elif problem == 'malformed':
                self.endpoint.invalid = True
            else:
                self.endpoint.status = problem
            first = await dispatcher.turn('shutdown pc', request_id=problem)
            self.assertEqual(dispatcher.last_route_event.outcome, expected)
            self.assertEqual(await dispatcher.turn('shutdown pc', request_id=problem), first)
            self.assertEqual(sum(name == 'AthenaPowerAction' for name, _ in self.endpoint.calls), 1)
            self.assertEqual(self.cloud.requests, [])

    async def test_unavailable_or_older_server_sends_no_action(self):
        for problem in ['missing_tool', 'missing_operation']:
            dispatcher = self.make()
            if problem == 'missing_tool':
                self.endpoint.tools = self.endpoint.tools[:1]
            else:
                self.endpoint.actions = ['restart']
            await dispatcher.turn('shutdown pc')
            self.assertEqual(dispatcher.last_route_event.outcome, 'not_started')
            self.assertFalse(any(name == 'AthenaPowerAction' for name, _ in self.endpoint.calls))
            self.assertEqual(self.cloud.requests, [])

    async def test_last_power_step_can_follow_a_local_light_command(self):
        dispatcher = self.make()
        reply = await dispatcher.turn('turn off nanoleafs then shutdown pc')
        self.assertIn('Step 2:', reply)
        self.assertEqual(len(self.ha.actions), 1)
        self.assertEqual(sum(name == 'AthenaPowerAction' for name, _ in self.endpoint.calls), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_fuzzy_app_target_can_precede_a_final_power_action(self):
        dispatcher = self.make()
        apps = SimulatedHA()
        dispatcher.apps_factory = lambda: apps
        reply = await dispatcher.turn('open no pad then restart pc')
        self.assertIn('Step 2:', reply)
        self.assertEqual(len(apps.actions), 1)
        self.assertEqual(sum(name == 'AthenaPowerAction' for name, _ in self.endpoint.calls), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_power_before_another_step_or_contextual_mixture_sends_nothing(self):
        dispatcher = self.make()
        for text in ['shutdown pc then turn off nanoleafs',
                     'wake pc and shutdown pc',
                     'shutdown pc and open my most recent project']:
            await dispatcher.turn(text)
            self.assertEqual(dispatcher.last_route_event.outcome, 'clarification')
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.ha.actions, [])
        self.assertEqual(self.cloud.requests, [])

    async def test_disabled_power_route_keeps_existing_cloud_behavior(self):
        dispatcher = self.make()
        dispatcher.power_enabled = False
        await dispatcher.turn('shutdown pc')
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.cloud.requests, ['shutdown pc'])

    def test_exact_grammar_stays_separate_from_speech_correction(self):
        self.assertEqual(route_reason(plan_command('turn off my computer'), True, True)[0], 'local')
        for text in ['Athena, turned off my computer.', 'I turned off my computer',
                     "don't shut down my pc"]:
            self.assertNotEqual(route_reason(plan_command(text), True, True)[0], 'local')


if __name__ == '__main__':
    unittest.main()
