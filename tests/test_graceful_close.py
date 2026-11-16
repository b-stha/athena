from copy import deepcopy
import json
import unittest

from mcp.types import CallToolResult, TextContent, Tool
from jsonschema import Draft202012Validator

from athena_agent.application_catalog import ApplicationCatalog
from athena_agent.config import AgentConfig, ServerConfig
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_apps import AppExecutionResult, AppNotSent, AppUnknownOutcome, DirectApps, LocalAppsConfig
from athena_agent.planning import cloud_tool_servers, plan_command
from athena_agent.tools import ToolError, ToolRegistry, ToolSpec
from router import Action, execute_action, parse_action
from routing_fakes import SimulatedCloud, SimulatedHA
from test_application_catalog import CATALOG_SCHEMA, FIXTURE, LAUNCH_SCHEMA


WINDOWS_SCHEMA = {'type': 'object', 'properties': {
    'application_id': {'type': 'string'}, 'revision': {'type': 'string'}},
    'required': ['application_id', 'revision'], 'additionalProperties': False}
CLOSE_SCHEMA = deepcopy(LAUNCH_SCHEMA)
CLOSE_SCHEMA['properties']['window_id'] = {'type': ['string', 'null']}


class Endpoint:
    def __init__(self):
        self.document = deepcopy(FIXTURE)
        for row in self.document['applications']:
            row['operations'].append('close')
        self.tools = [Tool(name=name, inputSchema=schema) for name, schema in [
            ('AthenaAppCatalog', CATALOG_SCHEMA), ('AthenaLaunchApp', LAUNCH_SCHEMA),
            ('AthenaAppWindows', WINDOWS_SCHEMA), ('AthenaCloseApp', CLOSE_SCHEMA)]]
        self.calls, self.effects = [], []
        self.rows = [{'id': 'win_' + '1' * 32, 'title': 'One fixture'}]
        self.status, self.reason = 'closed', 'window_destroyed'
        self.error, self.invalid = False, False

    def peer(self, *args):
        endpoint = self
        class Peer:
            async def start(self):
                return endpoint.tools
            async def call(self, name, args):
                endpoint.calls.append((name, deepcopy(args)))
                if name == 'AthenaAppCatalog':
                    document = deepcopy(endpoint.document)
                    error = False
                elif name == 'AthenaAppWindows':
                    document = {'schema_version': 1, **args, 'status': 'ready',
                                'windows': deepcopy(endpoint.rows)}
                    error = False
                elif name == 'AthenaCloseApp':
                    if endpoint.error:
                        raise TimeoutError('private endpoint detail')
                    status = endpoint.status
                    if len(endpoint.rows) > 1 and 'window_id' not in args:
                        status = 'clarification'
                    sent = status in {'closed', 'hidden', 'pending'}
                    document = {'schema_version': 1, **args, 'operation': 'close', 'status': status,
                                'reason': 'multiple_windows' if status == 'clarification' else endpoint.reason,
                                'message_sent': sent, 'window_closed': status == 'closed',
                                'windows': deepcopy(endpoint.rows) if status == 'clarification' else []}
                    document['window_id'] = args.get('window_id', endpoint.rows[0]['id'] if sent else None)
                    if sent:
                        endpoint.effects.append(deepcopy(args))
                    if endpoint.invalid:
                        document['window_closed'] = False
                    error = status not in {'closed', 'hidden'}
                else:
                    raise AssertionError('Unexpected tool: ' + name)
                return CallToolResult(content=[TextContent(type='text', text=json.dumps(document))],
                                      isError=error)
            async def close(self):
                pass
        return Peer()


class GrammarTests(unittest.TestCase):
    def test_close_exit_quit_use_the_catalog_and_preserve_scope(self):
        endpoint = Endpoint()
        catalog = ApplicationCatalog.from_document(endpoint.document)
        for text in ['close Notepad', 'exit Visual Studio Code', 'quit Google Chrome',
                     "close No Man's Sky"]:
            step, = plan_command(text, applications=catalog.vocabulary).steps
            self.assertEqual(step.route, 'local')
            self.assertEqual(step.action.action, 'close_app')
        for text in ["don't close Notepad", 'close Notepad after dinner',
                     'close Notepad or Google Chrome']:
            plan = plan_command(text, applications=catalog.vocabulary)
            self.assertFalse(all(row.route == 'local' for row in plan.steps))

    def test_close_context_cannot_use_other_windows_tools(self):
        self.assertEqual(cloud_tool_servers('Athena, close VSCode'), frozenset({'windows'}))
        self.assertEqual(cloud_tool_servers('could you please close my editor'), frozenset({'windows'}))

    def test_legacy_http_never_receives_a_close_action(self):
        from unittest.mock import patch
        with patch('integrations.desktop.execute') as backend:
            with self.assertRaises(ValueError):
                execute_action(parse_action('close Notepad'))
        backend.assert_not_called()


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.endpoint = Endpoint()
        self.apps = DirectApps(LocalAppsConfig(ServerConfig('windows', 'http://pc.test/mcp', 'private-token')),
                               peer_factory=self.endpoint.peer)
        self.ha, self.cloud = SimulatedHA(), SimulatedCloud()
        self.dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=True,
            apps_factory=lambda: self.apps, local_factory=lambda: self.ha,
            agent_factory=lambda: self.cloud, event_sink=lambda _: None)
        self.addAsyncCleanup(self.dispatcher.close)
        return self.dispatcher

    async def test_single_close_uses_no_model_and_replay_sends_no_second_close(self):
        dispatcher = self.make()
        reply = await dispatcher.turn('close Notepad', request_id='close')
        self.assertEqual(reply, 'Notepad window closed.')
        self.assertEqual(dispatcher.last_route_event.outcome, 'closed')
        self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
        before = deepcopy(self.endpoint.calls)
        self.assertEqual(await dispatcher.turn('close Notepad', request_id='close'), reply)
        self.assertEqual(self.endpoint.calls, before)
        self.assertEqual(len(self.endpoint.effects), 1)
        self.assertEqual(self.cloud.requests, [])
        self.assertNotIn('window_id', self.endpoint.effects[0])

    async def test_multiple_windows_need_explicit_numbered_reply_scoped_to_session(self):
        dispatcher = self.make()
        self.endpoint.rows.append({'id': 'win_' + '2' * 32, 'title': 'Second fixture'})
        text = await dispatcher.turn('close Notepad', session_id='room', request_id='which')
        self.assertIn('Multiple Notepad windows', text)
        self.assertEqual(self.endpoint.effects, [])
        self.assertEqual(await dispatcher.turn('close Notepad', session_id='room', request_id='which'), text)
        self.assertIn('No current window selection', await dispatcher.turn('close window two', session_id='other'))
        self.assertEqual(self.endpoint.effects, [])
        result = await dispatcher.turn('Athena, close window two.', session_id='room', request_id='select')
        self.assertEqual(result, 'Notepad window closed.')
        self.assertEqual(self.endpoint.effects[0]['window_id'], 'win_' + '2' * 32)
        self.assertEqual(await dispatcher.turn('Athena, close window two.', session_id='room', request_id='select'), result)
        self.assertEqual(len(self.endpoint.effects), 1)
        self.assertEqual(self.cloud.requests, [])

    async def test_old_choice_expires_and_any_new_command_cancels_it(self):
        dispatcher = self.make()
        self.endpoint.rows.append({'id': 'win_' + '2' * 32, 'title': 'Second'})
        await dispatcher.turn('close Notepad')
        pending = dispatcher.window_choices['default']
        dispatcher.window_choices['default'] = (0, *pending[1:])
        self.assertIn('No current window selection', await dispatcher.turn('window one'))
        await dispatcher.turn('close Notepad')
        await dispatcher.turn('turn on nanoleafs')
        self.assertIn('No current window selection', await dispatcher.turn('window one'))
        self.assertEqual(self.endpoint.effects, [])

    async def test_unknown_number_cannot_silently_pick_a_different_window(self):
        dispatcher = self.make()
        self.endpoint.rows.append({'id': 'win_' + '2' * 32, 'title': 'Second'})
        await dispatcher.turn('close Notepad')
        self.assertIn('from 1 to 2', await dispatcher.turn('close window 9'))
        self.assertEqual(self.endpoint.effects, [])
        await dispatcher.turn('window one')
        self.assertEqual(self.endpoint.effects[0]['window_id'], 'win_' + '1' * 32)

    async def test_pending_or_hidden_receipt_is_not_called_closed(self):
        for status in ['pending', 'hidden', 'not_started']:
            dispatcher = self.make()
            self.endpoint.status = status
            self.endpoint.reason = 'window_not_identified' if status == 'not_started' else 'fixture'
            reply = await dispatcher.turn('close Notepad', request_id=status)
            self.assertNotIn('window closed', reply)
            self.assertEqual(dispatcher.last_route_event.outcome, status)
            self.assertEqual(await dispatcher.turn('close Notepad', request_id=status), reply)
            self.assertEqual(self.cloud.requests, [])

    async def test_pending_close_stops_the_rest_of_a_local_compound(self):
        dispatcher = self.make()
        self.endpoint.status = 'pending'
        text = await dispatcher.turn('close Notepad then turn off nanoleafs')
        self.assertIn('Remaining actions stopped', text)
        self.assertEqual(self.ha.actions, [])
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(len(self.endpoint.effects), 1)

    async def test_ambiguous_close_compound_preflight_sends_nothing(self):
        dispatcher = self.make()
        self.endpoint.rows.append({'id': 'win_' + '2' * 32, 'title': 'Second'})
        reply = await dispatcher.turn('turn on nanoleafs and close Notepad')
        self.assertIn('No actions sent', reply)
        self.assertEqual(self.ha.actions, [])
        self.assertEqual(self.endpoint.effects, [])

    async def test_contextual_mixture_with_close_waits_for_the_next_workstream(self):
        dispatcher = self.make()
        for text in ['close Notepad and open my latest project',
                     'turn on nanoleafs and close my editor',
                     'close Notepad and open Google Chrome?']:
            reply = await dispatcher.turn(text)
            self.assertIn('separately', reply)
        self.assertEqual(self.endpoint.effects, [])
        self.assertEqual(self.ha.actions, [])
        self.assertEqual(self.cloud.requests, [])

    async def test_timeout_malformed_receipt_and_unknown_never_fall_back_or_retry(self):
        for problem in ['timeout', 'invalid', 'unknown']:
            dispatcher = self.make()
            if problem == 'timeout':
                self.endpoint.error = True
            elif problem == 'invalid':
                self.endpoint.invalid = True
            else:
                self.endpoint.status = 'unknown'
            first = await dispatcher.turn('close Notepad', request_id=problem)
            self.assertIn('unknown', first)
            self.assertEqual(await dispatcher.turn('close Notepad', request_id=problem), first)
            self.assertEqual(sum(name == 'AthenaCloseApp' for name, _ in self.endpoint.calls), 1)
            self.assertEqual(self.cloud.requests, [])

    async def test_older_extension_cannot_turn_close_into_legacy_launch_or_kill(self):
        dispatcher = self.make()
        for row in self.endpoint.document['applications']:
            row['operations'] = ['launch']
        reply = await dispatcher.turn('close Notepad')
        self.assertIn('No close sent', reply)
        self.assertEqual(self.endpoint.effects, [])
        self.assertEqual(self.cloud.requests, [])
        self.assertFalse(any(name != 'AthenaAppCatalog' for name, _ in self.endpoint.calls))


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_submit_two_close_effects_in_one_user_turn(self):
        from athena_agent.agent import Agent
        for batch in [True, False]:
            class Model:
                requests = 0
                async def close(self): pass
                async def complete(self, messages, tools):
                    self.requests += 1
                    def call(number):
                        return {'id': 'close-' + str(number), 'type': 'function', 'function': {
                            'name': 'windows__AthenaCloseApp', 'arguments': '{}'}}
                    return {'role': 'assistant', 'content': None, 'tool_calls':
                            [call(1), call(2)] if batch else [call(self.requests)]}
            class Registry:
                unavailable = {}
                def __init__(self): self.calls = []
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def definitions(self):
                    return [{'type': 'function', 'function': {'name': 'windows__AthenaCloseApp',
                                                              'parameters': {'type': 'object'}}}]
                async def call(self, *args):
                    self.calls.append(args)
                    return {'content': 'Window closed.', 'images': [], 'execution_status': 'returned'}
            registry, model = Registry(), Model()
            agent = Agent(AgentConfig('https://model.test/v1/chat/completions', 'fake', 'private-token'),
                          model=model, registry_factory=lambda _: registry)
            reply = await agent.turn('close VSCode')
            self.assertIn('Only one close attempt', reply)
            self.assertEqual(len(registry.calls), 0 if batch else 1)
            await agent.close()

    async def test_model_cannot_guess_an_explicit_window_selection(self):
        config = AgentConfig('https://model.test/v1/chat/completions', 'fake', 'private-token')
        registry = ToolRegistry(config)
        registry.tools['windows__AthenaCloseApp'] = ToolSpec('windows__AthenaCloseApp',
            'AthenaCloseApp', 'windows', 'normal close', CLOSE_SCHEMA, Draft202012Validator(CLOSE_SCHEMA))
        params = registry.definitions()[0]['function']['parameters']
        self.assertNotIn('window_id', params['properties'])
        with self.assertRaises(ToolError):
            await registry.call('windows__AthenaCloseApp',
                {'application_id': 'app_' + '1' * 32, 'revision': 'a' * 64,
                 'request_id': 'one', 'window_id': 'win_' + '1' * 32})

    async def test_close_context_rejects_a_model_power_shell_batch_before_effects(self):
        from athena_agent.agent import Agent
        class Model:
            async def close(self):
                pass
            async def complete(self, messages, tools):
                self.names = {tool['function']['name'] for tool in tools}
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'bad', 'type': 'function', 'function': {
                        'name': 'windows__PowerShell', 'arguments': '{"command":"Stop-Process"}'}}]}
        class Registry:
            unavailable = {}
            calls = []
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def definitions(self):
                return [{'type': 'function', 'function': {'name': name, 'parameters': {}}}
                        for name in ['windows__AthenaAppCatalog', 'windows__AthenaAppWindows',
                                     'windows__AthenaCloseApp', 'windows__PowerShell', 'windows__Process']]
            async def call(self, *args):
                self.calls.append(args)
                raise AssertionError('Forbidden batch was dispatched')
        model, registry = Model(), Registry()
        agent = Agent(AgentConfig('https://model.test/v1/chat/completions', 'fake', 'private-token'),
                      model=model, registry_factory=lambda _: registry)
        reply = await agent.turn('close VSCode')
        self.assertIn('allowed scope', reply)
        self.assertEqual(registry.calls, [])
        self.assertEqual(model.names, {'windows__AthenaAppCatalog', 'windows__AthenaAppWindows',
                                       'windows__AthenaCloseApp'})
        await agent.close()
