from pathlib import Path
import unittest
from unittest.mock import patch

from athena_agent.dispatcher import RequestDispatcher
from athena_agent.planning import plan_command
from athena_agent.routing_benchmark import SimulatedCloud, SimulatedHA, evaluate
from router import Action, UnknownCommand, parse_action


class LightPhrasingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_local(self, text, expected):
        backend = SimulatedHA()
        def no_cloud():
            raise AssertionError('Complete light requests must not initialize cloud')
        dispatcher = RequestDispatcher(local_enabled=True, local_factory=lambda: backend,
                                       agent_factory=no_cloud, event_sink=lambda event: None)
        try:
            with patch('socket.socket', side_effect=AssertionError('No network permitted')):
                await dispatcher.turn(text)
            self.assertEqual(backend.actions, [expected])
            self.assertEqual(dispatcher.last_route_event.route, 'local')
            self.assertEqual(dispatcher.last_route_event.model_request_attempts, 0)
        finally:
            await dispatcher.close()

    async def test_six_original_fallbacks_execute_exact_registered_actions_locally(self):
        cases = [('turn bathroom lights on', 'turn_on'),
                 ('switch the bathroom lights off', 'turn_off'),
                 ('could you turn off bathroom lights', 'turn_off'),
                 ('please turn on the bathroom lights', 'turn_on'),
                 ('bathroom lights off', 'turn_off'),
                 ('turn off bathroom lights?', 'turn_off')]
        for text, action in cases:
            with self.subTest(text=text):
                await self.assert_local(text, Action('home_assistant', action, 'light.bathroom'))

    async def test_new_paraphrases_generalize_to_existing_targets_and_both_states(self):
        cases = [
            ('Would you please switch the bedroom lights off?', 'turn_off', 'bedroom', 'area_id'),
            ('can you turn the bathroom lights on please', 'turn_on', 'light.bathroom', 'entity_id'),
            ('please could you switch off the underglow', 'turn_off', 'light.under_glow', 'entity_id'),
            ('please turn the table glow on', 'turn_on', 'light.table_glow', 'entity_id'),
            ('switch nanoleafs off please', 'turn_off', 'light.nanoleafs', 'entity_id'),
            ('the bedroom lights on please', 'turn_on', 'bedroom', 'area_id'),
            ('Hey Athena, could you turn off the table glow?', 'turn_off', 'light.table_glow', 'entity_id'),
            ('could you switch on the bathroom lights?', 'turn_on', 'light.bathroom', 'entity_id'),
            ('please bathroom lights on', 'turn_on', 'light.bathroom', 'entity_id'),
            ('turn the under glow off', 'turn_off', 'light.under_glow', 'entity_id'),
        ]
        for text, action, target, kind in cases:
            with self.subTest(text=text):
                await self.assert_local(text, Action('home_assistant', action, target, kind))

    async def test_negation_conditions_questions_and_extra_clauses_have_no_local_effects(self):
        cases = [
            "could you not turn off bathroom lights", "please don't turn bathroom lights on",
            'could you turn off bathroom lights only if I leave?',
            'bathroom lights off when I leave',
            'turn bathroom lights on unless I am asleep',
            'can bathroom lights turn off?', 'are bathroom lights off?',
            'could you explain how to turn off bathroom lights?',
            'bathroom lights off?', 'the bathroom lights off?',
            'would you prefer to turn off bathroom lights?',
            'turn? off bathroom lights', 'turn off bathroom lights??',
            'turn off bathroom lights? open notepad',
            'turn off bathroom lights. Open notepad',
            'please turn on bathroom lights and restart my pc',
            'turn bathroom lights on and off',
            'could you turn bathroom lights off or on?',
            'please turn off bathroom lights but leave bedroom on',
            'turn off those lights', 'turn off kitchen lights',
            'could you turn off bathroom light 1',
            'turn o nbathroom lights', 'turn of bathroom lights',
            'bathroom lights are off', 'she said turn bathroom lights on',
            'restart my pc?', 'could you restart my pc',
            'could you turn off bathroom lights before I leave?',
            'turn on bathroom lights and could you open my latest project',
        ]
        for text in cases:
            with self.subTest(text=text):
                backend, cloud = SimulatedHA(), SimulatedCloud()
                dispatcher = RequestDispatcher(local_enabled=True, local_factory=lambda: backend,
                    agent_factory=lambda: cloud, event_sink=lambda event: None)
                try:
                    with patch('socket.socket', side_effect=AssertionError('No network permitted')):
                        await dispatcher.turn(text)
                    self.assertEqual(backend.actions, [])
                    self.assertEqual(cloud.requests, [text])
                finally:
                    await dispatcher.close()

    def test_router_full_matches_do_not_guess_actions_or_new_targets(self):
        cases = ["could you not turn off bathroom lights", 'turn bathroom lights on and off',
                 'turn the kitchen lights off', 'bathroom lights are off',
                 'turn of bathroom lights', 'turn off bathroom light 1',
                 'could you turn on bathroom lights if I am home',
                 'bathroom lights off?', 'the bathroom lights on?',
                 'turn? off bathroom lights', 'turn off bathroom lights??', 'restart pc?']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(UnknownCommand):
                parse_action(text)

    async def test_updated_corpus_preserves_six_phrasing_fixes_and_compound_progress(self):
        fixture = Path(__file__).parent / 'fixtures/routing_requests.json'
        with patch('socket.socket', side_effect=AssertionError('No network permitted')):
            report = await evaluate(fixture)
        self.assertEqual(report['summary']['policy_mismatch_cases'], 0)
        self.assertEqual(report['summary']['observed_routes'], {'local': 17, 'cloud': 23, 'clarify': 3})
        self.assertEqual(report['summary']['desired_route_gap_cases'], 13)
        changed = [row for row in report['rows'] if row.get('baseline_route') and row['category'] == 'grammar_gap']
        self.assertEqual(len(changed), 6)
        for row in changed:
            self.assertEqual(row['baseline_route'], 'cloud')
            self.assertEqual(row['observed']['route'], 'local')
            self.assertEqual(row['observed']['model_request_attempts'], 0)
            self.assertEqual(row['simulated_actions'][0]['target'], 'light.bathroom')

    def test_punctuation_exception_preserves_source_spans(self):
        text = ' Hey Athena, could you turn the bathroom lights off? '
        plan = plan_command(text)
        step, = plan.steps
        self.assertEqual(plan.original, text)
        self.assertEqual(text[step.start:step.end], step.text)
        self.assertTrue(step.text.endswith('?'))
        self.assertEqual(step.route, 'local')


if __name__ == '__main__':
    unittest.main()
