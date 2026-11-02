import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_ha import DirectHA, HARejected, HAUnknownOutcome, LocalHAConfig
from router import Action, parse_action


class FakeHA:
    def __init__(self, error=None):
        self.error, self.calls, self.closed = error, [], False

    async def execute(self, action):
        self.calls.append(action)
        await asyncio.sleep(0)
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


class FakeCloud:
    def __init__(self):
        self.calls, self.context, self.closed = [], [], False

    def remember_local_turn(self, text, result, *, session_id):
        self.context.append((text, result, session_id))

    async def turn(self, text, session_id='default'):
        self.calls.append((text, session_id))
        return 'Contextual response'

    async def close(self):
        self.closed = True


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def make(self, error=None, *, enabled=True):
        self.ha, self.cloud = FakeHA(error), FakeCloud()
        self.cloud_factory = Mock(return_value=self.cloud)
        dispatcher = RequestDispatcher(local_enabled=enabled, local_factory=lambda: self.ha,
                                       agent_factory=self.cloud_factory)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_single_ha_turn_works_without_cloud_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            dispatcher = self.make()
            reply = await dispatcher.turn('Athena, turn on bathroom lights', request_id='one')
        self.assertEqual(reply, 'Requested bathroom lights on.')
        self.assertEqual(len(self.ha.calls), 1)
        self.assertEqual(dispatcher.ledger[('default', 'one')].status, 'accepted')
        self.cloud_factory.assert_not_called()

    async def test_rejections_and_unknown_outcomes_never_become_cloud_fallback(self):
        for error, status in [(HARejected(), 'rejected'), (HAUnknownOutcome(), 'unknown'),
                              (RuntimeError('unexpected'), 'unknown')]:
            with self.subTest(status=status):
                dispatcher = self.make(error)
                reply = await dispatcher.turn('turn off bedroom lights', request_id='failure')
                self.assertIn('not retried', reply)
                self.assertEqual(dispatcher.ledger[('default', 'failure')].status, status)
                self.assertEqual(len(self.ha.calls), 1)
                self.cloud_factory.assert_not_called()

    async def test_missing_local_configuration_does_not_spend_cloud_calls(self):
        dispatcher = self.make()
        dispatcher.local_factory = Mock(side_effect=ValueError('private configuration detail'))
        reply = await dispatcher.turn('turn on bathroom', request_id='badconfig')
        self.assertIn('No action sent', reply)
        self.assertNotIn('private configuration detail', reply)
        self.assertEqual(dispatcher.ledger[('default', 'badconfig')].status, 'not_started')
        self.cloud_factory.assert_not_called()
        self.assertEqual(self.ha.calls, [])

    async def test_same_request_id_is_not_dispatched_twice_even_concurrently(self):
        dispatcher = self.make(HAUnknownOutcome())
        replies = await asyncio.gather(*[
            dispatcher.turn('turn on bathroom', request_id='same') for _ in range(2)])
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(len(self.ha.calls), 1)
        reply = await dispatcher.turn('turn off bathroom', request_id='same')
        self.assertIn('different command', reply)
        self.assertEqual(len(self.ha.calls), 1)

    async def test_new_requests_with_identical_text_are_not_deduplicated(self):
        dispatcher = self.make()
        await dispatcher.turn('turn on bathroom')
        await dispatcher.turn('turn on bathroom')
        self.assertEqual(len(self.ha.calls), 2)

    async def test_mixed_pc_negation_and_context_requests_reach_cloud_whole(self):
        dispatcher = self.make()
        for text in ['turn on bathroom and open my most recent project',
                     'turn on bathroom then turn off bedroom', 'restart my pc',
                     "don't turn on bathroom", 'turn on the lights', 'turn it off']:
            with self.subTest(text=text):
                self.assertEqual(await dispatcher.turn(text, session_id='room'), 'Contextual response')
                self.assertEqual(self.cloud.calls[-1], (text, 'room'))
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(len(dispatcher.ledger), 0)

    async def test_disable_flag_preserves_cloud_route(self):
        dispatcher = self.make(enabled=False)
        await dispatcher.turn('turn on bathroom')
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual(self.ha.calls, [])

    async def test_missing_cloud_configuration_does_not_disable_later_local_requests(self):
        dispatcher = self.make()
        dispatcher.agent_factory = Mock(side_effect=ValueError('missing model key'))
        self.assertIn('Contextual routing is unavailable', await dispatcher.turn('open my latest project'))
        self.assertEqual(await dispatcher.turn('turn on bathroom'), 'Requested bathroom lights on.')
        self.assertEqual(len(self.ha.calls), 1)

    async def test_empty_or_wake_only_never_initializes_either_backend(self):
        dispatcher = self.make()
        for text in ['', 'Athena', 'Hey Athena!']:
            self.assertEqual(await dispatcher.turn(text), 'Please say a command.')
        self.assertIsNone(dispatcher.local)
        self.cloud_factory.assert_not_called()

    async def test_local_context_survives_later_cloud_initialization_and_stays_in_session(self):
        dispatcher = self.make(HAUnknownOutcome())
        await dispatcher.turn('turn on bathroom', session_id='room')
        await dispatcher.turn('what happened?', session_id='room')
        text, result, session = self.cloud.context[0]
        self.assertEqual((text, session), ('turn on bathroom', 'room'))
        self.assertIn('outcome=unknown', result)
        self.assertIn('Do not repeat', result)
        self.ha.error = None
        await dispatcher.turn('turn off bathroom', session_id='other')
        self.assertEqual(self.cloud.context[-1][2], 'other')
        self.assertIn('outcome=accepted', self.cloud.context[-1][1])

    async def test_cancellation_after_dispatch_records_unknown_and_cannot_retry_same_id(self):
        dispatcher = self.make()
        entered = asyncio.Event()
        async def blocked(action):
            self.ha.calls.append(action)
            entered.set()
            await asyncio.Event().wait()
        self.ha.execute = blocked
        task = asyncio.create_task(dispatcher.turn('turn on bathroom', request_id='cancel'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        reply = await dispatcher.turn('turn on bathroom', request_id='cancel')
        self.assertIn('unknown', reply)
        self.assertEqual(len(self.ha.calls), 1)
        self.cloud_factory.assert_not_called()

    async def test_ledger_and_pending_session_context_are_bounded(self):
        dispatcher = self.make()
        for index in range(130):
            await dispatcher.turn('turn on bathroom', request_id=f'request{index}', session_id=f'room{index % 9}')
        self.assertEqual(len(dispatcher.ledger), 128)
        self.assertEqual(len(dispatcher.pending_context), 8)
        self.assertTrue(all(len(turns) <= 6 for turns in dispatcher.pending_context.values()))


class DirectHATests(unittest.IsolatedAsyncioTestCase):
    async def send(self, handler, action=None):
        backend = DirectHA(LocalHAConfig('http://ha.test', 'private-token'),
                           transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(backend.close)
        await backend.execute(action or parse_action('turn on bathroom'))

    async def test_authenticated_entity_and_area_service_payloads(self):
        for text, key, value in [('turn on bathroom', 'entity_id', 'light.bathroom'),
                                 ('turn off bedroom lights', 'area_id', 'bedroom')]:
            seen = []
            def handler(request):
                seen.append(request)
                self.assertEqual(request.headers['authorization'], 'Bearer private-token')
                self.assertEqual(json.loads(request.content), {key: value})
                return httpx.Response(200, json=[])
            action = parse_action(text)
            await self.send(handler, action)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].url.path, f'/api/services/light/{action.action}')

    async def test_rejection_server_error_redirect_and_malformed_result_do_not_retry(self):
        for status, body, error in [(401, {}, HARejected), (429, {}, HARejected),
                                    (500, {}, HAUnknownOutcome), (408, {}, HAUnknownOutcome),
                                    (302, {}, HAUnknownOutcome), (200, {}, HAUnknownOutcome)]:
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(status, json=body, headers={'Location': 'http://other.test'})
            with self.subTest(status=status), self.assertRaises(error):
                await self.send(handler)
            self.assertEqual(len(seen), 1)

    async def test_timeout_and_disconnect_are_unknown_without_retry(self):
        for error in [httpx.ReadTimeout('private detail'), httpx.RemoteProtocolError('private detail')]:
            seen = []
            def handler(request):
                seen.append(request)
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(HAUnknownOutcome) as result:
                await self.send(handler)
            self.assertNotIn('private detail', str(result.exception))
            self.assertEqual(len(seen), 1)

    async def test_unregistered_and_non_ha_actions_cannot_reach_http(self):
        handler = Mock(side_effect=AssertionError('No request permitted'))
        for action in [Action('home_assistant', 'turn_on', 'light.unknown'), parse_action('restart pc')]:
            with self.assertRaises(ValueError):
                await self.send(handler, action)
        handler.assert_not_called()

    async def test_non_json_success_response_is_unknown(self):
        with self.assertRaises(HAUnknownOutcome):
            await self.send(lambda request: httpx.Response(200, text='<html>login</html>'))

    def test_configuration_rejects_invalid_origins_and_hides_token(self):
        for url in ['', 'ftp://ha.test', 'http://u:p@ha.test', 'http://ha.test/api',
                    'http://ha.test?x=1', 'http://ha.test:bad', 'http://ha. test']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalHAConfig(url, 'token')
        self.assertNotIn('private-token', repr(LocalHAConfig('http://ha.test', 'private-token')))


class AgentLocalHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_result_reaches_real_agent_history_without_a_model_request(self):
        from athena_agent.agent import Agent
        from athena_agent.config import AgentConfig
        class Model:
            def __init__(self):
                self.messages = []
            async def complete(self, messages, tools):
                self.messages.append(messages)
                return {'content': 'The earlier request was accepted.'}
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
        agent = Agent(AgentConfig('https://model.test/chat/completions', 'test', 'test-key', history_turns=2),
                      model=model, registry_factory=lambda config: Registry())
        self.addAsyncCleanup(agent.close)
        for index in range(3):
            agent.remember_local_turn(f'local request {index}', 'outcome=accepted; already requested', session_id='room')
        self.assertEqual(model.messages, [])
        self.assertEqual(len(agent.history['room']), 2)
        await agent.turn('What happened?', session_id='room')
        messages = model.messages[0]
        self.assertTrue(any(m.get('content') == 'local request 2' for m in messages))
        self.assertFalse(any(m.get('content') == 'local request 0' for m in messages))
        await agent.turn('What happened?', session_id='other')
        self.assertFalse(any(m.get('content') == 'local request 2' for m in model.messages[1]))


@contextmanager
def fake_ha_http(status=200, disconnect=False):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            seen.append((self.path, self.headers.get('Authorization'),
                         json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            if disconnect:
                self.connection.close()
                return
            body = b'[]'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_service_accept_reject_failure_and_disconnect(self):
        for status, disconnect, expected in [(200, False, 'accepted'), (401, False, 'rejected'),
                                               (500, False, 'unknown'), (200, True, 'unknown')]:
            with self.subTest(status=status, disconnect=disconnect), fake_ha_http(status, disconnect) as (url, seen):
                cloud = Mock(side_effect=AssertionError('No cloud allowed'))
                dispatcher = RequestDispatcher(local_enabled=True, agent_factory=cloud,
                    local_factory=lambda: DirectHA(LocalHAConfig(url, 'fake-token')))
                try:
                    first = await dispatcher.turn('turn on bathroom', request_id='http')
                    self.assertEqual(first, await dispatcher.turn('turn on bathroom', request_id='http'))
                    self.assertEqual(dispatcher.ledger[('default', 'http')].status, expected)
                    self.assertEqual(seen, [('/api/services/light/turn_on', 'Bearer fake-token',
                                            {'entity_id': 'light.bathroom'})])
                    cloud.assert_not_called()
                finally:
                    await dispatcher.close()


class EntryPointTests(unittest.TestCase):
    def test_text_mode_uses_local_dispatch_without_model_settings(self):
        import main
        backend = FakeHA()
        with patch.dict(os.environ, {'ATHENA_LOCAL_HA_ENABLED': 'true'}, clear=True), \
                patch('athena_agent.dispatcher.DirectHA', return_value=backend), \
                patch('athena_agent.dispatcher.LocalHAConfig.from_env'), \
                patch('athena_agent.dispatcher.cloud_agent', side_effect=AssertionError('No model')), \
                patch('builtins.input', side_effect=['turn on bathroom', 'exit']), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main.text_mode(), 0)
        self.assertIn('Requested bathroom lights on', output.getvalue())
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(backend.closed)

    def test_voice_worker_uses_same_local_dispatch_without_model_settings(self):
        from athena_agent.worker import AgentWorker
        backend, done, results, errors = FakeHA(), threading.Event(), [], []
        def result(text):
            results.append(text)
            done.set()
        def error(text):
            errors.append(text)
            done.set()
        with patch.dict(os.environ, {'ATHENA_LOCAL_HA_ENABLED': 'true'}, clear=True), \
                patch('athena_agent.dispatcher.DirectHA', return_value=backend), \
                patch('athena_agent.dispatcher.LocalHAConfig.from_env'), \
                patch('athena_agent.dispatcher.cloud_agent', side_effect=AssertionError('No model')):
            worker = AgentWorker()
            try:
                self.assertTrue(worker.submit('turn off bathroom', result, error))
                self.assertTrue(done.wait(3))
            finally:
                worker.close()
        self.assertEqual(errors, [])
        self.assertEqual(results, ['Requested bathroom lights off.'])
        self.assertTrue(backend.closed)


if __name__ == '__main__':
    unittest.main()
