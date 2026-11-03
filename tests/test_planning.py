import json
import unittest
from unittest.mock import patch

from athena_agent.planning import plan_command


class PlanningTests(unittest.TestCase):
    def plan(self, text):
        return plan_command(text, request_id='request01')

    def test_single_known_command_preserves_source_and_action(self):
        text = '  Hey Athena, TURN ON bathroom lights!  '
        plan = self.plan(text)
        self.assertEqual(plan.original, text)
        step, = plan.steps
        self.assertEqual(step.route, 'local')
        self.assertEqual(step.action.target, 'light.bathroom')
        self.assertEqual(text[step.start:step.end], step.text)
        self.assertFalse(plan.as_dict()['execution_enabled'])

    def test_mixed_request_keeps_contextual_remainder_and_full_request(self):
        text = 'turn on bedroom lights and open my most recent project'
        plan = self.plan(text)
        self.assertEqual([s.route for s in plan.steps], ['local', 'contextual'])
        self.assertEqual(plan.steps[1].text, 'open my most recent project')
        self.assertEqual(plan.steps[0].action.target_type, 'area_id')
        self.assertEqual(plan.original, text)
        for step in plan.steps:
            self.assertEqual(text[step.start:step.end], step.text)

    def test_generic_lights_are_not_assigned_an_invented_default(self):
        plan = self.plan('turn on the lights and open my most recent project')
        self.assertEqual([s.route for s in plan.steps], ['contextual', 'contextual'])
        self.assertTrue(all(s.action is None for s in plan.steps))

    def test_ordering_dependencies_have_stable_per_request_identifiers(self):
        plan = self.plan('turn on bedroom lights and then open notepad then turn off bathroom')
        self.assertEqual([s.route for s in plan.steps], ['local'] * 3)
        self.assertEqual(plan.steps[1].depends_on, ('request01:1',))
        self.assertEqual(plan.steps[2].depends_on, ('request01:2',))
        self.assertEqual(self.plan(plan.original), plan)
        json.dumps(plan.as_dict())

    def test_local_step_after_contextual_step_keeps_its_dependency(self):
        plan = self.plan('find my most recent project then open notepad')
        self.assertEqual([s.route for s in plan.steps], ['contextual', 'local'])
        self.assertEqual(plan.steps[1].depends_on, (plan.steps[0].step_id,))

    def test_semicolon_and_politeness_preserve_the_complete_clauses(self):
        text = 'Please turn on bathroom; please open notepad.'
        plan = self.plan(text)
        self.assertEqual([s.route for s in plan.steps], ['local', 'local'])
        self.assertEqual(plan.steps[1].connector, ';')
        self.assertEqual(plan.steps[0].text, 'Please turn on bathroom')
        self.assertEqual(plan.steps[1].text, 'please open notepad.')

    def test_scope_guards_preserve_whole_request_without_local_candidates(self):
        cases = [
            "don't turn on bedroom lights and open notepad",
            'turn on bedroom lights and don’t open notepad',
            'turn on bedroom lights only if I am home',
            'if I am home turn on bedroom lights and open notepad',
            'turn on bedroom lights and open notepad before I arrive',
            'turn on bedroom lights but actually turn off bathroom',
            'turn on bedroom lights or turn off bathroom',
            'turn on bedroom lights and turn it off',
            'turn on bedroom lights and open "turn off bathroom"',
            'turn on bedroom lights. Open notepad',
            'are bedroom lights on?',
            'turn on bedroom lights and stop',
        ]
        for text in cases:
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertEqual(plan.scope_strategy, 'whole_request')
                self.assertEqual(len(plan.steps), 1)
                self.assertEqual(plan.steps[0].route, 'contextual')
                self.assertIsNone(plan.steps[0].action)

    def test_noun_coordination_and_incomplete_clauses_are_not_partial_actions(self):
        for text in ('turn on bedroom and bathroom', 'open rock and roll',
                     'turn on bedroom lights and',
                     'turn on bedroom lights and maybe open notepad',
                     'turn on bedroom and bathroom and open notepad'):
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertEqual(len(plan.steps), 1)
                self.assertEqual(plan.steps[0].reason, 'unresolved_clause_boundary')
                self.assertIsNone(plan.steps[0].action)

    def test_plan_keeps_repeated_and_opposing_actions_distinct(self):
        plan = self.plan('turn on bathroom then turn off bathroom')
        self.assertEqual([s.action.action for s in plan.steps], ['turn_on', 'turn_off'])
        self.assertNotEqual(plan.steps[0].step_id, plan.steps[1].step_id)

    def test_empty_input_does_not_become_cloud_work(self):
        for text in ('', '  ', 'Athena', 'Hey Athena!'):
            with self.subTest(text=text):
                self.assertEqual(self.plan(text).steps[0].route, 'clarify')

    def test_limits_reject_invalid_requests_and_bound_step_count(self):
        for text in (None, 12, 'x' * 8001):
            with self.subTest(text=text if not isinstance(text, str) else 'overlong'):
                with self.assertRaises(ValueError):
                    self.plan(text)
        with self.assertRaises(ValueError):
            plan_command('open notepad', request_id='bad:id')
        plan = self.plan(' and '.join(['open notepad'] * 9))
        self.assertEqual(plan.steps[0].reason, 'plan_step_limit')

    def test_preview_never_dispatches_backend_requests(self):
        with patch('router.execute_action', side_effect=AssertionError('Must not execute')), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Must not connect')):
            self.plan('turn on bathroom and open my most recent project')
            self.plan('restart my pc then open notepad')


if __name__ == '__main__':
    unittest.main()
