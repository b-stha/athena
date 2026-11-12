"""Retained request IDs must not repeat effects across local/contextual routes."""

import asyncio
from types import SimpleNamespace
import unittest

from athena_agent.dispatcher import RequestDispatcher
from athena_agent.tools import ToolRegistry
from athena_agent.config import AgentConfig
from mcp.types import CallToolResult, TextContent


class Cloud:
    def __init__(self):
        self.calls = []
        self.last_execution = None

    async def turn(self, text, session_id='default'):
        self.calls.append((text, session_id))
        await asyncio.sleep(0)
        return 'Contextual response.'

    def remember_local_turn(self, *args, **kwargs):
        pass

    async def close(self):
        pass


class Local:
    def __init__(self):
        self.calls = []

    async def execute(self, action):
        self.calls.append(action)

    async def close(self):
        pass


class ExecutionLedgerTests(unittest.IsolatedAsyncioTestCase):
    def make(self, cloud=None, **kwargs):
        self.cloud = cloud or Cloud()
        self.local = Local()
        self.events = []
        dispatcher = RequestDispatcher(agent_factory=lambda: self.cloud, local_enabled=True,
            apps_enabled=True, local_factory=lambda: self.local, apps_factory=lambda: self.local,
            event_sink=self.events.append, **kwargs)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_cloud_duplicate_returns_original_without_new_model_or_effect(self):
        dispatcher = self.make()
        first = await dispatcher.turn('open my latest project', request_id='same')
        second = await dispatcher.turn('open my latest project', request_id='same')
        self.assertEqual(first, second)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual((self.events[-1].route, self.events[-1].model_request_attempts), ('replay', 0))

    async def test_concurrent_identical_ids_execute_once(self):
        dispatcher = self.make()
        replies = await asyncio.gather(*(dispatcher.turn('open my latest project', request_id='same') for _ in range(4)))
        self.assertEqual(len(set(replies)), 1)
        self.assertEqual(len(self.cloud.calls), 1)

    async def test_cloud_id_conflict_blocks_local_route_and_reverse(self):
        for first, second in [('open my latest project', 'turn on nanoleafs'),
                              ('turn on nanoleafs', 'open my latest project')]:
            with self.subTest(first=first):
                dispatcher = self.make()
                await dispatcher.turn(first, request_id='same')
                reply = await dispatcher.turn(second, request_id='same')
                self.assertIn('already used', reply)
                self.assertEqual(len(self.local.calls) + len(self.cloud.calls), 1)
                self.assertEqual(self.events[-1].route, 'blocked')

    async def test_distinct_sessions_or_ids_are_distinct_requests(self):
        dispatcher = self.make()
        for session, identifier in [('one', 'same'), ('two', 'same'), ('one', 'new')]:
            await dispatcher.turn('open my latest project', session_id=session, request_id=identifier)
        self.assertEqual(len(self.cloud.calls), 3)

    async def test_cloud_configuration_failure_is_retained(self):
        dispatcher = self.make()
        attempts = []
        def unavailable():
            attempts.append(1)
            raise ValueError('private-token')
        dispatcher.agent_factory = unavailable
        first = await dispatcher.turn('open my latest project', request_id='same')
        second = await dispatcher.turn('open my latest project', request_id='same')
        self.assertEqual(first, second)
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('private-token', first)
        self.assertEqual(dispatcher.ledger[('default', 'same')].status, 'configuration_unavailable')

    async def test_unexpected_cloud_failure_after_effect_is_cached_unknown(self):
        cloud = Cloud()
        async def failed(text, session_id='default'):
            cloud.calls.append((text, session_id))
            raise RuntimeError('private-token')
        cloud.turn = failed
        dispatcher = self.make(cloud)
        first = await dispatcher.turn('open my latest project', request_id='same')
        second = await dispatcher.turn('open my latest project', request_id='same')
        self.assertEqual(first, second)
        self.assertIn('unknown', first)
        self.assertNotIn('private-token', first)
        self.assertEqual(len(cloud.calls), 1)

    async def test_cancelled_cloud_effect_is_never_replayed(self):
        cloud, entered = Cloud(), asyncio.Event()
        async def blocked(text, session_id='default'):
            cloud.calls.append((text, session_id))
            entered.set()
            await asyncio.Event().wait()
        cloud.turn = blocked
        dispatcher = self.make(cloud)
        task = asyncio.create_task(dispatcher.turn('open my latest project', request_id='same'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        reply = await dispatcher.turn('open my latest project', request_id='same')
        self.assertIn('unknown', reply)
        self.assertEqual(len(cloud.calls), 1)

    async def test_structured_partial_outcome_is_copied_and_retained(self):
        cloud = Cloud()
        tool = SimpleNamespace(name='windows__App', status='returned')
        async def partial(text, session_id='default'):
            cloud.calls.append((text, session_id))
            cloud.last_execution = SimpleNamespace(session_id=session_id, text=text.strip(),
                status='failed', reply='windows__App returned; model HTTP 429.', tool_records=(tool,))
            return cloud.last_execution.reply
        cloud.turn = partial
        dispatcher = self.make(cloud)
        first = await dispatcher.turn(' open my latest project ', request_id='same')
        self.assertEqual(dispatcher.ledger[('default', 'same')].tool_records, (tool,))
        self.assertEqual(self.events[-1].outcome, 'failed')
        self.assertEqual(await dispatcher.turn(' open my latest project ', request_id='same'), first)
        self.assertEqual(len(cloud.calls), 1)

    async def test_previous_snapshot_cannot_hide_a_new_failed_turn(self):
        cloud = Cloud()
        cloud.last_execution = SimpleNamespace(session_id='default', text='open my latest project',
            status='returned', reply='Old successful turn.', tool_records=())
        async def failed(*args, **kwargs):
            raise RuntimeError('new failed turn')
        cloud.turn = failed
        dispatcher = self.make(cloud)
        reply = await dispatcher.turn('open my latest project', request_id='new')
        self.assertIn('unknown', reply)
        self.assertNotIn('Old successful', reply)

    async def test_other_session_snapshot_is_not_used(self):
        cloud = Cloud()
        async def wrong_session(*args, **kwargs):
            cloud.last_execution = SimpleNamespace(session_id='other', text='open my latest project',
                status='returned', reply='Other session.', tool_records=())
            raise RuntimeError()
        cloud.turn = wrong_session
        dispatcher = self.make(cloud)
        self.assertIn('unknown', await dispatcher.turn('open my latest project'))

    async def test_invalid_custom_agent_result_is_unknown_and_retained(self):
        cloud = Cloud()
        async def invalid(text, session_id='default'):
            cloud.calls.append((text, session_id))
            return None
        cloud.turn = invalid
        dispatcher = self.make(cloud)
        first = await dispatcher.turn('open my latest project', request_id='same')
        self.assertIn('unknown', first)
        self.assertEqual(await dispatcher.turn('open my latest project', request_id='same'), first)
        self.assertEqual(len(cloud.calls), 1)

    async def test_history_failure_cannot_hide_an_accepted_local_action(self):
        cloud = Cloud()
        def broken(*args, **kwargs):
            raise RuntimeError('history failure')
        cloud.remember_local_turn = broken
        dispatcher = self.make(cloud)
        dispatcher.agent = cloud
        first = await dispatcher.turn('turn on nanoleafs', request_id='same')
        self.assertEqual(first, 'Requested nanoleafs lights on.')
        self.assertEqual(await dispatcher.turn('turn on nanoleafs', request_id='same'), first)
        self.assertEqual(len(self.local.calls), 1)

    async def test_clarification_reserves_id_and_blocks_later_different_action(self):
        dispatcher = self.make()
        await dispatcher.turn('Athena', request_id='same')
        reply = await dispatcher.turn('turn on nanoleafs', request_id='same')
        self.assertIn('already used', reply)
        self.assertEqual(self.local.calls, [])

    async def test_cloud_ledger_is_bounded_and_eviction_does_not_claim_durability(self):
        dispatcher = self.make()
        for index in range(130):
            await dispatcher.turn('open my latest project', request_id=f'id-{index}')
        self.assertEqual(len(dispatcher.ledger), 128)
        self.assertNotIn(('default', 'id-0'), dispatcher.ledger)

    def test_reported_mcp_error_is_typed_unknown_not_success(self):
        registry = ToolRegistry(AgentConfig('https://model.example/v1', 'fake', 'fake-key'))
        for failed, status in [(True, 'unknown'), (False, 'returned')]:
            result = registry.format_result('windows__App', CallToolResult(isError=failed,
                content=[TextContent(type='text', text='Server response')]))
            self.assertEqual(result['execution_status'], status)


if __name__ == '__main__':
    unittest.main()
