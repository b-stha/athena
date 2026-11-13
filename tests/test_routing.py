import asyncio
from dataclasses import asdict
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from athena_agent.agent import Agent
from athena_agent.config import AgentConfig
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_ha import HAUnknownOutcome
from athena_agent.routing import emit_route_event
from tools.routing_benchmark import SimulatedCloud, SimulatedHA, evaluate


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, enabled=True, cloud=None, sink=None):
        self.backend = SimulatedHA()
        self.cloud = SimulatedCloud() if cloud is None else cloud
        self.events = []
        dispatcher = RequestDispatcher(local_enabled=enabled, local_factory=lambda: self.backend,
            agent_factory=lambda: self.cloud, event_sink=self.events.append if sink is None else sink)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_local_event_contains_counts_and_no_command_target_session_or_response(self):
        dispatcher = self.make()
        await dispatcher.turn('turn on bathroom lights', session_id='private-session')
        event = self.events[0]
        self.assertEqual((event.route, event.reason, event.outcome),
                         ('local', 'complete_registered_ha_command', 'accepted'))
        self.assertEqual((event.model_request_attempts, event.local_action_attempts), (0, 1))
        encoded = json.dumps(asdict(event))
        for private in ('bathroom', 'private-session', 'Requested', 'turn on'):
            self.assertNotIn(private, encoded)
        self.assertGreaterEqual(event.elapsed_ms, 0)
        self.assertIs(dispatcher.last_route_event, event)

    async def test_policy_reasons_differentiate_disabled_mixed_scope_and_backend(self):
        for text, enabled, expected in [
            ('turn on bathroom', False, 'local_ha_disabled'),
            ('turn on bathroom and open notepad', True, 'compound_execution_not_integrated'),
            ("don't turn on bathroom", True, 'negation'),
            ('open notepad', True, 'local_backend_not_integrated'),
            ('turn o nbathroom lights', True, 'unresolved_command_or_target')]:
            with self.subTest(text=text):
                dispatcher = self.make(enabled=enabled)
                await dispatcher.turn(text)
                event = self.events[-1]
                self.assertEqual((event.route, event.reason), ('cloud', expected))
                self.assertEqual(event.model_request_attempts, 1)
                self.assertEqual(event.local_action_attempts, 0)

    async def test_unknown_outcome_replay_and_conflict_do_not_record_new_attempts(self):
        dispatcher = self.make()
        async def fail(action):
            raise HAUnknownOutcome()
        self.backend.execute = fail
        await dispatcher.turn('turn on bathroom', request_id='one')
        self.assertEqual(self.events[-1].outcome, 'unknown')
        await dispatcher.turn('turn on bathroom', request_id='one')
        self.assertEqual((self.events[-1].route, self.events[-1].local_action_attempts), ('replay', 0))
        await dispatcher.turn('turn off bathroom', request_id='one')
        self.assertEqual((self.events[-1].route, self.events[-1].reason), ('blocked', 'request_id_conflict'))

    async def test_missing_configuration_reports_no_attempts(self):
        dispatcher = self.make()
        def unavailable():
            raise ValueError('private-secret')
        dispatcher.local_factory = unavailable
        await dispatcher.turn('turn on bathroom')
        self.assertEqual((self.events[-1].outcome, self.events[-1].local_action_attempts), ('not_started', 0))
        dispatcher.agent_factory = unavailable
        await dispatcher.turn('open my project')
        self.assertEqual((self.events[-1].outcome, self.events[-1].model_request_attempts),
                         ('configuration_unavailable', 0))
        self.assertNotIn('private-secret', repr(self.events))

    async def test_unknown_model_count_is_not_reported_as_zero(self):
        class UncountedCloud:
            async def turn(self, *args, **kwargs):
                return 'result'
            async def close(self):
                pass
        dispatcher = self.make(cloud=UncountedCloud())
        await dispatcher.turn('open my project')
        self.assertIsNone(self.events[-1].model_request_attempts)

    async def test_sink_failure_cannot_change_local_result_or_trigger_repeat(self):
        def broken(event):
            raise RuntimeError('diagnostic disk unavailable')
        dispatcher = self.make(sink=broken)
        self.assertEqual(await dispatcher.turn('turn on bathroom'), 'Requested bathroom lights on.')
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_cancellation_emits_unknown_local_outcome(self):
        dispatcher = self.make()
        entered = asyncio.Event()
        async def blocked(action):
            entered.set()
            await asyncio.Event().wait()
        self.backend.execute = blocked
        task = asyncio.create_task(dispatcher.turn('turn on bathroom'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((self.events[-1].outcome, self.events[-1].local_action_attempts), ('unknown', 1))

    async def test_diagnostic_output_is_opt_in_json(self):
        dispatcher = self.make()
        await dispatcher.turn('turn on bathroom')
        event = self.events[0]
        for setting in ('false', 'true'):
            with self.subTest(setting=setting), patch.dict('os.environ', {'ATHENA_ROUTE_LOG': setting}), \
                    patch('sys.stderr', new_callable=io.StringIO) as output:
                emit_route_event(event)
                if setting == 'false':
                    self.assertEqual(output.getvalue(), '')
                else:
                    value = json.loads(output.getvalue().removeprefix('[athena.route] '))
                    self.assertEqual(value['route'], 'local')

    async def test_authored_request_suite_matches_policy_and_exposes_coverage_gaps(self):
        fixture = Path(__file__).parent / 'fixtures/routing_requests.json'
        with patch('socket.socket', side_effect=AssertionError('No network permitted')):
            report = await evaluate(fixture)
        self.assertEqual(report['summary']['policy_mismatch_cases'], 0)
        self.assertGreater(report['summary']['desired_route_gap_cases'], 0)
        self.assertEqual(len(report['rows']), report['summary']['cases'])
        self.assertTrue(report['synthetic_model_attempt_counts'])

    async def test_real_agent_counts_all_model_round_attempts(self):
        class Model:
            calls = 0
            async def complete(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {'role': 'assistant', 'tool_calls': [{'id': 'fake-call', 'type': 'function',
                        'function': {'name': 'ha__fake', 'arguments': '{}'}}]}
                return {'role': 'assistant', 'content': 'done'}
            async def close(self):
                pass
        class Registry:
            unavailable = {}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def definitions(self):
                return []
            async def call(self, name, arguments):
                return {'content': 'Simulated acceptance', 'images': []}
        agent = Agent(AgentConfig('https://model.test/chat/completions', 'test', 'test'),
                      model=Model(), registry_factory=lambda _: Registry())
        dispatcher = self.make(cloud=agent)
        await dispatcher.turn('explain my project')
        await dispatcher.turn('explain my project again')
        self.assertEqual(agent.model_request_attempts, 3)
        self.assertEqual([event.model_request_attempts for event in self.events], [2, 1])

    async def test_model_error_still_counts_one_attempt_without_retry(self):
        from athena_agent.model import ModelError
        class Model:
            calls = 0
            async def complete(self, *args):
                self.calls += 1
                raise ModelError('Cloud model returned HTTP 429.')
            async def close(self):
                pass
        class Registry:
            unavailable = {}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def definitions(self):
                return []
        model = Model()
        agent = Agent(AgentConfig('https://model.test/chat/completions', 'test', 'test'),
                      model=model, registry_factory=lambda _: Registry())
        dispatcher = self.make(cloud=agent)
        self.assertIn('429', await dispatcher.turn('open my latest project'))
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.events[-1].model_request_attempts, 1)


if __name__ == '__main__':
    unittest.main()
