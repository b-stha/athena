import asyncio
import unittest
from unittest.mock import Mock

from mcp.types import Tool
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_apps import DirectApps, LocalAppsConfig
from athena_agent.local_ha import HomeAssistantMCP, LocalHAConfig, HANotSent, HARejected
from ha_mcp_fixture import Peer
from athena_agent.config import ServerConfig
from router import parse_action


class Backend:
    def __init__(self, events):
        self.events, self.error, self.check_error = events, None, None

    async def preflight(self, action):
        self.events.append(('check', action))
        if self.check_error:
            raise self.check_error

    async def execute(self, action):
        self.events.append(('send', action))
        if self.error:
            raise self.error

    async def close(self):
        pass


class CompoundTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        self.events = []
        self.ha, self.apps = Backend(self.events), Backend(self.events)
        self.cloud = Mock(side_effect=AssertionError('No model permitted'))
        dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=True,
            local_factory=lambda:self.ha, apps_factory=lambda:self.apps,
            agent_factory=self.cloud, event_sink=lambda _:None, **settings)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_cross_backend_preflight_precedes_all_effects_and_replay_is_cached(self):
        d = self.make()
        text = 'turn on nanoleafs and open Notepad'
        reply = await d.turn(text, request_id='same')
        self.assertEqual([kind for kind,_ in self.events], ['check','check','send','send'])
        self.assertEqual([action.backend for kind,action in self.events if kind=='send'], ['home_assistant','desktop'])
        self.assertIn('Step 1:', reply)
        self.assertEqual([s.status for s in d.ledger[('default','same')].step_records], ['accepted','accepted'])
        self.assertEqual(d.last_route_event.model_request_attempts, 0)
        self.assertEqual(await d.turn(text, request_id='same'), reply)
        self.assertEqual(len(self.events), 4)
        self.cloud.assert_not_called()

    async def test_opposite_actions_and_three_step_fuzzy_targets_preserve_order(self):
        d = self.make()
        await d.turn('turn on nanoleafs then turn off nanoleafs and then open node pad', request_id='order')
        actions = [a.action for kind,a in self.events if kind=='send']
        self.assertEqual(actions, ['turn_on','turn_off','launch_app'])
        rows = d.ledger[('default','order')].step_records
        self.assertEqual(rows[1].depends_on, (rows[0].step_id,))
        self.assertEqual(rows[2].depends_on, (rows[1].step_id,))

    async def test_later_configuration_failure_sends_nothing(self):
        d = self.make()
        d.apps_factory = Mock(side_effect=ValueError('private-secret'))
        reply = await d.turn('turn on nanoleafs and open Notepad')
        self.assertIn('No actions sent', reply)
        self.assertEqual(self.events, [])
        self.assertNotIn('private-secret', reply)

    async def test_later_preflight_failure_sends_nothing_and_caches_result(self):
        d = self.make()
        self.apps.check_error = OSError()
        text = 'turn on nanoleafs and open Notepad'
        first = await d.turn(text, request_id='fail')
        self.assertIn('No actions sent', first)
        self.assertFalse(any(kind=='send' for kind,_ in self.events))
        self.assertEqual(await d.turn(text, request_id='fail'), first)
        self.assertEqual(len(self.events), 2)

    async def test_unknown_first_action_stops_even_independent_and(self):
        d = self.make()
        self.ha.error = TimeoutError()
        await d.turn('turn on nanoleafs and open Notepad', request_id='fail')
        rows = d.ledger[('default','fail')].step_records
        self.assertEqual([r.status for r in rows], ['unknown','not_started'])
        self.assertEqual(d.last_route_event.local_action_attempts, 1)

    async def test_rejected_second_step_preserves_first_and_skips_third(self):
        d = self.make()
        self.ha.error = HARejected()
        text = 'open Notepad then turn on nanoleafs then open Notepad'
        first = await d.turn(text, request_id='partial')
        self.assertEqual([r.status for r in d.ledger[('default','partial')].step_records],
                         ['accepted','rejected','not_started'])
        self.assertEqual(d.last_route_event.outcome, 'partial')
        self.assertIn('Notepad launch accepted', first)
        self.assertEqual(await d.turn(text, request_id='partial'), first)
        self.assertEqual(sum(kind=='send' for kind,_ in self.events), 2)

    async def test_ha_not_sent_after_first_step_preserves_partial_result(self):
        d = self.make()
        self.ha.error = HANotSent()
        text = 'open Notepad then turn on nanoleafs then open Notepad'
        first = await d.turn(text, request_id='not-sent')
        self.assertEqual([row.status for row in d.ledger[('default', 'not-sent')].step_records],
                         ['accepted', 'not_started', 'not_started'])
        self.assertEqual(d.last_route_event.outcome, 'partial')
        self.assertEqual(await d.turn(text, request_id='not-sent'), first)
        self.assertEqual(sum(kind == 'send' for kind, _ in self.events), 2)
        self.cloud.assert_not_called()

    async def test_cancellation_after_first_action_preserves_outcomes_for_retry(self):
        d = self.make()
        entered = asyncio.Event()
        async def blocked(action):
            self.events.append(('send', action))
            entered.set()
            await asyncio.Event().wait()
        self.apps.execute = blocked
        text = 'turn on nanoleafs then open Notepad'
        task = asyncio.create_task(d.turn(text, request_id='cancel'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        reply = await d.turn(text, request_id='cancel')
        self.assertIn('Requested nanoleafs', reply)
        self.assertIn('unknown', reply)
        self.assertEqual(sum(kind=='send' for kind,_ in self.events), 2)

    async def test_cancelled_preflight_sends_nothing(self):
        d = self.make()
        entered = asyncio.Event()
        async def blocked(action):
            entered.set()
            await asyncio.Event().wait()
        self.apps.preflight = blocked
        task = asyncio.create_task(d.turn('turn on nanoleafs and open Notepad', request_id='cancel'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('No actions sent', await d.turn('turn on nanoleafs and open Notepad', request_id='cancel'))
        self.assertFalse(any(k=='send' for k,_ in self.events))

    async def test_four_known_steps_clarify_without_cloud_or_effects(self):
        d = self.make()
        reply = await d.turn('turn on nanoleafs then open Notepad then turn off nanoleafs then open Notepad')
        self.assertIn('three', reply)
        self.assertEqual(self.events, [])
        self.cloud.assert_not_called()

    async def test_mixed_disabled_reference_and_correction_requests_stay_whole(self):
        for text in ['turn on nanoleafs and open my latest project',
                     'turn on nanoleafs then turn them off',
                     'turn on nanoleafs but actually turn off nanoleafs',
                     'turn on nanoleafs only if I am home', 'turn on nanoleafs and bathroom']:
            d = self.make()
            class Cloud:
                async def turn(self, value, session_id='default'):
                    self.text = value
                    return 'contextual'
                async def close(self): pass
            cloud = Cloud()
            d.agent_factory = lambda: cloud
            await d.turn(text)
            self.assertEqual(cloud.text, text)
            self.assertEqual(self.events, [])
        d = self.make()
        d.apps_enabled = False
        d.agent_factory = lambda: cloud
        await d.turn('turn on nanoleafs and open Notepad')
        self.assertEqual(self.events, [])

    async def test_semicolon_and_concurrent_same_id(self):
        d = self.make()
        text = 'turn on nanoleafs; open Notepad'
        replies = await asyncio.gather(*(d.turn(text, request_id='same') for _ in range(3)))
        self.assertEqual(len(set(replies)), 1)
        self.assertEqual(sum(kind=='send' for kind,_ in self.events), 2)

    async def test_ha_preflight_only_reads_mcp_inventory(self):
        peer = Peer()
        backend = HomeAssistantMCP(LocalHAConfig(ServerConfig('ha', 'http://ha.test/mcp', 'test-token')),
                                   peer_factory=lambda *args: peer)
        await backend.preflight(parse_action('turn on nanoleafs'))
        self.assertEqual(peer.calls, [('homeassistant__GetLiveContext', {'domain': 'light'})])
        self.assertEqual(peer.effects, [])
        self.assertTrue(peer.closed)

    async def test_windows_preflight_checks_schema_and_closes_without_app_call(self):
        for schema in [{'type':'object'}, {'type':'object','required':['impossible']}]:
            class Peer:
                closed = False
                async def start(self): return [Tool(name='App', inputSchema=schema)]
                async def call(self,*args): raise AssertionError('No App call in preflight')
                async def close(self): self.closed = True
            peer = Peer()
            backend = DirectApps(LocalAppsConfig(ServerConfig('windows','http://pc.test/mcp','test-token')),
                                 peer_factory=lambda *args:peer)
            if 'required' in schema:
                with self.assertRaises(ValueError): await backend.preflight(parse_action('open Notepad'))
            else:
                await backend.preflight(parse_action('open Notepad'))
            self.assertTrue(peer.closed)
