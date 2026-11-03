"""Conservative command-plan previews. No model, MCP or executor is invoked."""

import argparse
from dataclasses import asdict, dataclass
import json
import re
from uuid import uuid4

from router import (Action, UnknownCommand, application_scope_view, is_explicit_light_request,
                    normalize_command, parse_action, parse_fuzzy_action)
from resolver import AmbiguousTarget


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    start: int
    end: int
    text: str
    route: str
    reason: str
    action: Action | None = None
    connector: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandPlan:
    request_id: str
    original: str
    scope_strategy: str
    steps: tuple[PlanStep, ...]

    def as_dict(self):
        return {'schema_version': 1, 'mode': 'preview', 'execution_enabled': False,
                **asdict(self)}


# These guards deliberately over-defer. Scope must be resolved before extracting
# any candidate action, even when an earlier clause matches the local grammar.
SCOPE_GUARDS = (
    ('negation', r"\b(?:not|never|no|don't|dont|do\s+not|cannot|can't|cant|won't|wont|without)\b"),
    ('condition_or_timing', r'\b(?:if|unless|until|when|whenever|while|before|after|only|except)\b'),
    ('correction_or_alternative', r'\b(?:but|or|instead|rather|actually|cancel|stop|forget|ignore)\b'),
    ('unresolved_reference', r'\b(?:it|them|that|those|these|same|again|previous)\b'),
)
VERBS = r'(?:turn|switch|open|launch|start|wake|power|shutdown|shut|restart|reboot|sleep|suspend|put|send|show|find|search|tell)'
BOUNDARY = re.compile(
    rf'(?:\s+(?P<word>and\s+then|and|then)\s+|(?P<semicolon>;)\s*)'
    rf'(?=(?:please\s+)?{VERBS}\b)', re.IGNORECASE)
CONNECTOR_WORD = re.compile(r'\b(?:and|then)\b|;', re.IGNORECASE)
WAKE_PREFIX = re.compile(r'^\s*(?:hey\s+athena|athena)\b[\s,.:!]*', re.IGNORECASE)


def _span(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _scope_reason(text):
    view = text.casefold().replace('\u2019', "'")
    for reason, pattern in SCOPE_GUARDS:
        if re.search(pattern, view):
            return reason
    if re.search(r'["\u201c\u201d()\[\]]', text) or re.search(r"(?<!\w)'|'(?!\w)", text):
        return 'quoted_or_nested_scope'
    if re.search(r'[.!]\s+\S', text):
        return 'question_or_sentence_scope'
    if '?' in text and not (text.count('?') == 1 and text.rstrip().endswith('?')
                            and is_explicit_light_request(text)):
        return 'question_or_sentence_scope'
    return None


def plan_command(text, *, request_id=None):
    """Preserve source spans and expose grammar matches without executing them.

    Mixed plans require a future semantic/execution preflight. A local step is
    a parsed candidate, never permission to dispatch it independently.
    """
    if not isinstance(text, str) or len(text) > 8000:
        raise ValueError('A request must be text with at most 8000 characters.')
    request_id = uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id):
        raise ValueError('request_id must be a short alphanumeric identifier.')
    wake = WAKE_PREFIX.match(text)
    start, end = _span(text, wake.end() if wake else 0, len(text))

    def whole(route, reason):
        step = PlanStep(f'{request_id}:1', start, end, text[start:end], route, reason)
        return CommandPlan(request_id, text, 'whole_request', (step,))

    body = text[start:end]
    if not normalize_command(body):
        return whole('clarify', 'empty_or_wake_only')
    reason = _scope_reason(application_scope_view(body))
    if reason:
        return whole('contextual', reason)

    boundaries = list(BOUNDARY.finditer(text, start, end))
    # A noun joined by "and", an incomplete command, or an unrecognized right
    # clause must not be silently dropped while extracting a matching action.
    covered = {(match.start(), match.end()) for match in boundaries}
    if any(not any(left <= match.start() and match.end() <= right for left, right in covered)
           for match in CONNECTOR_WORD.finditer(text, start, end)):
        return whole('contextual', 'unresolved_clause_boundary')
    if len(boundaries) >= 8:
        return whole('contextual', 'plan_step_limit')

    steps = []
    cursor, connector = start, None
    for index, boundary in enumerate([*boundaries, None], 1):
        left, right = _span(text, cursor, boundary.start() if boundary else end)
        source = text[left:right]
        try:
            action = parse_action(source)
            route, reason = 'local', 'complete_registered_command'
        except UnknownCommand:
            try:
                action = parse_fuzzy_action(source)
                route, reason = 'local', 'fuzzy_registered_target'
            except AmbiguousTarget:
                action = None
                route, reason = 'clarify', 'ambiguous_registered_target'
            except UnknownCommand:
                action = None
                route, reason = 'contextual', 'unresolved_command_or_target'
        dependencies = (steps[-1].step_id,) if steps and connector in {'then', 'and then'} else ()
        steps.append(PlanStep(f'{request_id}:{index}', left, right, source, route, reason,
                              action, connector, dependencies))
        if boundary:
            cursor = boundary.end()
            connector = ' '.join(boundary.group('word').casefold().split()) if boundary.group('word') else ';'
    return CommandPlan(request_id, text, 'explicit_clauses' if boundaries else 'whole_request', tuple(steps))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('text', help='Request to preview; no actions are executed.')
    args = parser.parse_args(argv)
    try:
        plan = plan_command(args.text)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(plan.as_dict(), indent=2))


if __name__ == '__main__':
    main()
