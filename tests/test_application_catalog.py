"""Catalog routing, scope, refresh and uncertain execution use no real endpoints."""

from copy import deepcopy
import json
import unittest

from mcp.types import CallToolResult, TextContent, Tool

from athena_agent.application_catalog import ApplicationCatalog
from athena_agent.config import ServerConfig
from athena_agent.dispatcher import RequestDispatcher
from athena_agent.local_apps import AppNotSent, AppUnknownOutcome, DirectApps, LocalAppsConfig
from athena_agent.planning import cloud_tool_servers, needs_application_catalog, plan_command
from router import Action, parse_action, validate_action
from routing_fakes import SimulatedCloud, SimulatedHA


def entry(number, name, *, aliases=(), operations=('launch',)):
    return {'id': 'app_' + f'{number:032x}', 'display_name': name,
            'aliases': list(aliases), 'operations': list(operations)}


def document(*entries, revision='a' * 64):
    return {'schema_version': 1, 'revision': revision, 'applications': list(entries)}


FIXTURE = document(entry(1, 'Notepad'), entry(2, 'Visual Studio Code'),
                   entry(3, '7-Zip'), entry(4, "No Man's Sky"),
                   entry(5, 'Research and Development'), entry(6, 'Mix and Open'),
                   entry(7, 'C++ Editor'), entry(8, 'Google Chrome', aliases=('Chrome',)))
CATALOG_SCHEMA = {'type': 'object', 'properties': {'refresh': {'type': 'boolean'}},
                  'additionalProperties': False}
LAUNCH_SCHEMA = {'type': 'object', 'properties': {name: {'type': 'string'}
                 for name in ('application_id', 'revision', 'request_id')},
                 'required': ['application_id', 'revision', 'request_id'],
                 'additionalProperties': False}


class Endpoint:
    def __init__(self):
        self.document = deepcopy(FIXTURE)
        self.calls, self.closed, self.connections = [], 0, 0
        self.error, self.launch_status, self.wrong_receipt = None, 'accepted', False
        self.tools = [Tool(name='AthenaAppCatalog', inputSchema=CATALOG_SCHEMA),
                      Tool(name='AthenaLaunchApp', inputSchema=LAUNCH_SCHEMA)]

    def peer(self, *args):
        endpoint = self
        class Peer:
            async def start(self):
                endpoint.connections += 1
                if endpoint.error == 'connect':
                    raise ConnectionError('private transport details')
                return endpoint.tools
            async def call(self, name, arguments):
                endpoint.calls.append((name, deepcopy(arguments)))
                if endpoint.error == name:
                    raise TimeoutError('private transport details')
                if name == 'AthenaAppCatalog':
                    result = deepcopy(endpoint.document)
                else:
                    result = {'schema_version': 1, **arguments, 'window_ready': False,
                              'status': endpoint.launch_status, 'reason': 'fixture'}
                    if endpoint.wrong_receipt:
                        result['application_id'] = entry(99, 'Other')['id']
                return CallToolResult(content=[TextContent(type='text', text=json.dumps(result))])
            async def close(self):
                endpoint.closed += 1
        return Peer()


class VocabularyTests(unittest.TestCase):
    def test_exact_names_aliases_punctuation_and_numeric_names_use_windows_ids(self):
        catalog = ApplicationCatalog.from_document(FIXTURE)
        for phrase, number in [('open Visual Studio Code', 2), ('launch 7-Zip', 3),
                               ('open the 7 zip', 3), ("start No Man's Sky", 4),
                               ('open C++ Editor', 7), ('open Chrome', 8)]:
            with self.subTest(phrase=phrase):
                plan = plan_command(phrase, applications=catalog.vocabulary)
                self.assertEqual(plan.steps[0].route, 'local')
                self.assertEqual(plan.steps[0].action.target, entry(number, '')['id'])

    def test_catalog_is_immutable_and_action_validation_requires_membership(self):
        catalog = ApplicationCatalog.from_document(FIXTURE)
        with self.assertRaises(TypeError):
            catalog.vocabulary['other'] = ('x',)
        action = parse_action('open Chrome', applications=catalog.vocabulary)
        self.assertEqual(validate_action(action, applications=catalog.vocabulary), action)
        for other in [Action('desktop', 'launch_app', entry(99, '')['id'], 'application'),
                      Action('desktop', 'launch_app', r'C:\Windows\cmd.exe', 'application')]:
            with self.assertRaises(ValueError):
                validate_action(other, applications=catalog.vocabulary)

    def test_duplicate_display_names_or_aliases_require_clarification(self):
        for duplicate in [entry(9, 'Notepad'), entry(9, 'Other', aliases=('notepad',))]:
            catalog = ApplicationCatalog.from_document(document(*FIXTURE['applications'], duplicate))
            plan = plan_command('open notepad', applications=catalog.vocabulary)
            self.assertEqual((plan.steps[0].route, plan.steps[0].reason),
                             ('clarify', 'ambiguous_registered_target'))

    def test_new_apps_do_not_change_the_default_global_registry(self):
        catalog = ApplicationCatalog.from_document(FIXTURE)
        self.assertEqual(plan_command('open Chrome', applications=catalog.vocabulary).steps[0].route, 'local')
        self.assertEqual(plan_command('open Chrome').steps[0].route, 'contextual')

    def test_spoken_symbols_keep_notepad_and_notepad_plus_plus_distinct(self):
        catalog = ApplicationCatalog.from_document(document(*FIXTURE['applications'], entry(9, 'Notepad++')))
        for phrase, number in [('open no pad', 1), ('open node pad', 1),
                               ('open notepad++', 9), ('open notepad plus plus', 9),
                               ('open c plus plus editor', 7)]:
            with self.subTest(phrase=phrase):
                step, = plan_command(phrase, applications=catalog.vocabulary).steps
                self.assertEqual(step.route, 'local')
                self.assertEqual(step.action.target, entry(number, '')['id'])
        competing = ApplicationCatalog.from_document(document(entry(1, 'Notepad'), entry(2, 'Notepod')))
        self.assertEqual(plan_command('open node pad', applications=competing.vocabulary).steps[0].route,
                         'clarify')

    def test_nonlaunchable_app_is_not_a_deterministic_target(self):
        catalog = ApplicationCatalog.from_document(document(entry(1, 'Notepad', operations=('inspect',))))
        self.assertEqual(plan_command('open Notepad', applications=catalog.vocabulary).steps[0].route, 'contextual')

    def test_bounds_invalid_ids_duplicate_ids_names_and_versions_are_rejected(self):
        bad_documents = [document(entry(1, 'x'), entry(1, 'y')),
                         document(entry(1, 'x' * 161)), document(entry(1, '')),
                         document(entry(1, 'bad\nname')), document(*[entry(i, 'x') for i in range(2001)])]
        bad = deepcopy(FIXTURE)
        bad['applications'][0]['id'] = r'C:\Windows\notepad.exe'
        bad_documents.append(bad)
        bad = deepcopy(FIXTURE)
        bad['schema_version'] = True
        bad_documents.append(bad)
        for value in bad_documents:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                ApplicationCatalog.from_document(value)

    def test_app_name_conjunctions_do_not_split_the_target_and_spans_stay_exact(self):
        catalog = ApplicationCatalog.from_document(FIXTURE)
        for text in ['open Research and Development then turn off nanoleafs',
                     'Athena, open Mix and Open and turn on nanoleafs',
                     "turn on nanoleafs and then open No Man's Sky"]:
            with self.subTest(text=text):
                plan = plan_command(text, applications=catalog.vocabulary)
                self.assertEqual([step.route for step in plan.steps], ['local', 'local'])
                self.assertTrue(all(text[step.start:step.end] == step.text for step in plan.steps))

    def test_scope_outside_registered_names_is_never_hidden(self):
        catalog = ApplicationCatalog.from_document(FIXTURE)
        for text in ["don't open Notepad", "open No Man's Sky only if I'm home",
                     'open Chrome or Notepad', 'open Visual Studio Code then cancel that',
                     'open Chrome and show my latest project', 'open Chrome?',
                     'open "Notepad"', 'open Research and Development after dinner',
                     'open Chrome and Notepad', 'open notepad2']:
            with self.subTest(text=text):
                plan = plan_command(text, applications=catalog.vocabulary)
                self.assertFalse(all(step.route == 'local' for step in plan.steps))

    def test_catalog_read_is_scoped_to_app_clauses(self):
        for text in ['open Notepad', 'Athena, launch Chrome', 'turn on nanoleafs and open Code']:
            self.assertTrue(needs_application_catalog(text))
        for text in ['turn on nanoleafs', "don't open Notepad", 'what is the time',
                     'tell me how to open Notepad', 'Athena', 'open the bedroom blinds']:
            self.assertFalse(needs_application_catalog(text))
        self.assertIsNone(cloud_tool_servers('turn on nanoleafs and open Zed'))
        self.assertEqual(cloud_tool_servers('open the bedroom blinds'), frozenset({'ha'}))


class CatalogExecutionTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.endpoint = Endpoint()
        self.now = 0
        backend = DirectApps(LocalAppsConfig(ServerConfig('windows', 'http://pc.test/mcp', 'private-token')),
                             peer_factory=self.endpoint.peer, clock=lambda: self.now)
        self.addAsyncCleanup(backend.close)
        return backend

    async def test_cache_expiry_and_forced_refresh_read_only(self):
        backend = self.make()
        first = await backend.catalog()
        self.assertIs(first, await backend.catalog())
        self.assertEqual(len(self.endpoint.calls), 1)
        self.now = 61
        await backend.catalog()
        await backend.catalog(refresh=True)
        self.assertEqual([name for name, _ in self.endpoint.calls], ['AthenaAppCatalog'] * 3)
        self.assertEqual(self.endpoint.closed, self.endpoint.connections)

    async def test_launch_uses_only_id_revision_and_request_id(self):
        backend = self.make()
        catalog = await backend.catalog()
        action = parse_action('open Visual Studio Code', applications=catalog.vocabulary)
        await backend.execute_request(action, request_id='turn-one:1')
        name, args = self.endpoint.calls[-1]
        self.assertEqual(name, 'AthenaLaunchApp')
        self.assertEqual(args, {'application_id': entry(2, '')['id'], 'revision': catalog.revision,
                                'request_id': 'turn-one:1'})
        self.assertEqual(backend.display_name(action), 'Visual Studio Code')

    async def test_preflight_refreshes_catalog_without_launch(self):
        backend = self.make()
        catalog = await backend.catalog()
        await backend.preflight(parse_action('open Notepad', applications=catalog.vocabulary))
        self.assertEqual([name for name, _ in self.endpoint.calls], ['AthenaAppCatalog'] * 2)
        self.assertEqual(self.endpoint.calls[-1][1], {'refresh': True})

    async def test_changed_catalog_stops_preflight_before_any_launch(self):
        backend = self.make()
        catalog = await backend.catalog()
        self.endpoint.document['revision'] = 'b' * 64
        with self.assertRaises(AppNotSent):
            await backend.preflight(parse_action('open Notepad', applications=catalog.vocabulary))
        self.assertFalse(any(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls))

    async def test_missing_one_tool_or_incompatible_schema_does_not_use_legacy(self):
        for problem in ['missing', 'schema']:
            backend = self.make()
            if problem == 'missing':
                self.endpoint.tools.pop()
            else:
                self.endpoint.tools[1] = Tool(name='AthenaLaunchApp',
                                             inputSchema={'type': 'object', 'required': ['path']})
            with self.subTest(problem=problem), self.assertRaises(AppNotSent):
                await backend.catalog()
            self.assertEqual(self.endpoint.calls, [])

    async def test_not_started_receipt_invalidates_cache_without_retry(self):
        backend = self.make()
        catalog = await backend.catalog()
        action = parse_action('open Notepad', applications=catalog.vocabulary)
        self.endpoint.launch_status = 'not_started'
        with self.assertRaises(AppNotSent):
            await backend.execute(action)
        self.assertEqual(sum(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls), 1)
        await backend.catalog()
        self.assertEqual(sum(name == 'AthenaAppCatalog' for name, _ in self.endpoint.calls), 2)

    async def test_timeout_unknown_and_wrong_receipt_never_retry(self):
        for problem in ['timeout', 'unknown', 'receipt']:
            backend = self.make()
            catalog = await backend.catalog()
            action = parse_action('open Notepad', applications=catalog.vocabulary)
            if problem == 'timeout':
                self.endpoint.error = 'AthenaLaunchApp'
            elif problem == 'unknown':
                self.endpoint.launch_status = 'unknown'
            else:
                self.endpoint.wrong_receipt = True
            with self.subTest(problem=problem), self.assertRaises(AppUnknownOutcome):
                await backend.execute(action)
            self.assertEqual(sum(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls), 1)

    async def test_unregistered_or_shell_targets_rejected_before_connection(self):
        backend = self.make()
        await backend.catalog()
        before = self.endpoint.connections
        for target in [entry(99, '')['id'], 'cmd /c calc', r'C:\Windows\cmd.exe', 'notepad']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                await backend.execute(Action('desktop', 'launch_app', target, 'application'))
        self.assertEqual(self.endpoint.connections, before)


class CatalogDispatchTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.endpoint, self.cloud, self.ha = Endpoint(), SimulatedCloud(), SimulatedHA()
        backend = DirectApps(LocalAppsConfig(ServerConfig('windows', 'http://pc.test/mcp', 'private-token')),
                             peer_factory=self.endpoint.peer)
        dispatcher = RequestDispatcher(local_enabled=True, apps_enabled=True,
            local_factory=lambda: self.ha, apps_factory=lambda: backend,
            agent_factory=lambda: self.cloud, event_sink=lambda _: None)
        self.addAsyncCleanup(dispatcher.close)
        return dispatcher

    async def test_new_application_and_replay_need_zero_model_calls_and_no_repeat_discovery(self):
        dispatcher = self.make()
        text = 'Athena, open Visual Studio Code'
        first = await dispatcher.turn(text, request_id='same')
        self.assertIn('Visual Studio Code launch accepted', first)
        self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
        before = list(self.endpoint.calls)
        self.assertEqual(await dispatcher.turn(text, request_id='same'), first)
        self.assertEqual(self.endpoint.calls, before)
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(dispatcher.last_route_event.route, 'replay')

    async def test_conflicting_id_does_no_catalog_read_or_launch(self):
        dispatcher = self.make()
        await dispatcher.turn('open Chrome', request_id='same')
        before = list(self.endpoint.calls)
        reply = await dispatcher.turn('open Notepad', request_id='same')
        self.assertIn('different command', reply)
        self.assertEqual(self.endpoint.calls, before)

    async def test_plain_ha_turn_never_contacts_windows(self):
        dispatcher = self.make()
        await dispatcher.turn('turn on nanoleafs')
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(len(self.ha.actions), 1)

    async def test_contextual_ha_opening_does_not_depend_on_windows(self):
        dispatcher = self.make()
        self.endpoint.error = 'connect'
        await dispatcher.turn('open the bedroom blinds')
        self.assertEqual(self.endpoint.connections, 0)
        self.assertEqual(self.cloud.requests, ['open the bedroom blinds'])

    async def test_compound_preflights_all_and_passes_app_step_id(self):
        dispatcher = self.make()
        reply = await dispatcher.turn('turn on nanoleafs and open Chrome', request_id='compound')
        self.assertIn('Google Chrome launch accepted', reply)
        self.assertEqual(len(self.ha.actions), 1)
        self.assertTrue(self.endpoint.calls[-1][1]['request_id'].endswith(':compound:2'))
        self.assertEqual(self.cloud.requests, [])

    async def test_catalog_failure_stops_compound_before_ha_and_never_falls_back(self):
        dispatcher = self.make()
        self.endpoint.error = 'connect'
        reply = await dispatcher.turn('turn on nanoleafs and open Chrome')
        self.assertIn('No actions sent', reply)
        self.assertEqual(self.ha.actions, [])
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(dispatcher.last_route_event.outcome, 'not_started')
        await dispatcher.turn('turn on nanoleafs')
        self.assertEqual(len(self.ha.actions), 1)

    async def test_request_ids_in_different_sessions_have_distinct_windows_ids(self):
        dispatcher = self.make()
        for session in ['one', 'two']:
            await dispatcher.turn('open Notepad', session_id=session, request_id='same')
        requests = [args['request_id'] for name, args in self.endpoint.calls if name == 'AthenaLaunchApp']
        self.assertEqual(len(set(requests)), 2)

    async def test_unknown_app_and_contextual_modifier_keep_whole_request_for_model(self):
        dispatcher = self.make()
        for text in ['open my latest project', 'turn on nanoleafs and open a project',
                     "open Notepad but don't launch it"]:
            await dispatcher.turn(text)
            self.assertEqual(self.cloud.requests[-1], text)
        self.assertEqual(self.ha.actions, [])
        self.assertFalse(any(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls))

    async def test_ambiguous_name_clarifies_without_model_or_action(self):
        dispatcher = self.make()
        self.endpoint.document['applications'].append(entry(99, 'Notepad'))
        reply = await dispatcher.turn('open Notepad')
        self.assertEqual(reply, 'Please repeat the full target name.')
        self.assertEqual(self.cloud.requests, [])
        self.assertFalse(any(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls))

    async def test_unknown_launch_receipt_does_not_reach_model_or_retry_on_same_id(self):
        dispatcher = self.make()
        self.endpoint.launch_status = 'unknown'
        first = await dispatcher.turn('open Chrome', request_id='uncertain')
        self.assertIn('unknown', first)
        self.assertEqual(await dispatcher.turn('open Chrome', request_id='uncertain'), first)
        self.assertEqual(sum(name == 'AthenaLaunchApp' for name, _ in self.endpoint.calls), 1)
        self.assertEqual(self.cloud.requests, [])
