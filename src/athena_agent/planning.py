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
VERBS = r'(?:turn|switch|open|launch|start|close|exit|quit|wake|power|shutdown|shut|restart|reboot|sleep|suspend|put|send|show|find|search|tell)'
BOUNDARY = re.compile(
    rf'(?:\s+(?P<word>and\s+then|and|then)\s+|(?P<semicolon>;)\s*)'
    rf'(?=(?:please\s+)?{VERBS}\b)', re.IGNORECASE)
CONNECTOR_WORD = re.compile(r'\b(?:and|then)\b|;', re.IGNORECASE)
WAKE_PREFIX = re.compile(r'^\s*(?:hey\s+athena|athena)\b[\s,.:!]*', re.IGNORECASE)
HA_HINT = re.compile(r'\b(?:lights?|lamps?|fans?|thermostat|nanoleafs?|nanoleaves|'
                     r'table glow|under ?glow|bathroom|bedroom|blinds?|curtains?|shades?|garage door|gates?)\b')
HA_OPEN_HINT = re.compile(r'\b(?:blinds?|curtains?|shades?|garage door|gates?)\b', re.IGNORECASE)
PC_HINT = re.compile(r'\b(?:pc|computer|desktop|windows|notepad|screen|screenshot|'
                     r'monitor|apps?|applications?|programs?|projects?|files?|folders?|'
                     r'browser|chrome|edge|discord|steam|powershell|scripts?)\b')


def cloud_tool_servers(text):
    """Bound an explicit HA request; mixed/other requests keep their capabilities.

    This chooses tool access, never an action or a replacement target. A model
    may interpret a light request, but it cannot explore Windows to resolve it.
    """
    if is_app_close_request(text):
        return frozenset({'windows'})
    normalized = normalize_command(text)
    if (HA_HINT.search(normalized) and not PC_HINT.search(normalized)
            and not needs_application_catalog(text)):
        return frozenset({'ha'})
    return None


def is_app_close_request(text):
    body = WAKE_PREFIX.sub('', text, count=1).strip()
    return re.match(r'(?:(?:can|could|would) you )?(?:please )?(?:close|exit|quit)\s+\S',
                    body, re.IGNORECASE) is not None


def _unclear_switch_target(text):
    normalized = normalize_command(text)
    # Short, unqualified switch requests cannot identify their domain. Do not
    # let a misheard proper noun expand into unrelated application discovery.
    return (re.fullmatch(r'(?:(?:can|could|would) you )?(?:please )?'
                         r'(?:turn|switch) (?:on|off) (?:the )?'
                         r'[a-z]+(?: [a-z]+){0,2}', normalized) is not None
            and not HA_HINT.search(normalized) and not PC_HINT.search(normalized))


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


def needs_application_catalog(text):
    """Read-only discovery is needed only for an application clause."""
    body = WAKE_PREFIX.sub('', text, count=1)
    boundaries = list(BOUNDARY.finditer(body))
    starts = [0, *(boundary.end() for boundary in boundaries)]
    ends = [*(boundary.start() for boundary in boundaries), len(body)]
    for start, end in zip(starts, ends):
        clause = body[start:end].strip()
        if re.match(r'(?:please\s+)?(?:open|launch|start|close|exit|quit)\s+\S', clause, re.IGNORECASE):
            # "Open the blinds" needs HA context even when the PC is off.
            # An explicit app/PC cue keeps a Windows lookup available.
            if not HA_OPEN_HINT.search(clause) or PC_HINT.search(normalize_command(clause)):
                return True
    return False


def plan_command(text, *, request_id=None, applications=None):
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
    if re.match(r'^(?:and|then)\b', normalize_command(body)):
        return whole('clarify', 'incomplete_command')
    if re.match(r'^(?:on|off)\b', normalize_command(body)) and HA_HINT.search(normalize_command(body)):
        return whole('clarify', 'incomplete_command')
    preliminary = list(BOUNDARY.finditer(body))
    scope_body = application_scope_view(body, applications=applications, boundaries=preliminary)
    reason = _scope_reason(scope_body)
    if reason:
        return whole('contextual', reason)

    # Registered application names may contain conjunctions or command words.
    # Masked slots keep their original lengths, so source spans stay exact.
    scope_text = text[:start] + scope_body + text[end:]
    boundaries = list(BOUNDARY.finditer(scope_text, start, end))
    # A noun joined by "and", an incomplete command, or an unrecognized right
    # clause must not be silently dropped while extracting a matching action.
    covered = {(match.start(), match.end()) for match in boundaries}
    if any(not any(left <= match.start() and match.end() <= right for left, right in covered)
           for match in CONNECTOR_WORD.finditer(scope_text, start, end)):
        return whole('contextual', 'unresolved_clause_boundary')
    if len(boundaries) >= 8:
        return whole('contextual', 'plan_step_limit')

    steps = []
    cursor, connector = start, None
    for index, boundary in enumerate([*boundaries, None], 1):
        left, right = _span(text, cursor, boundary.start() if boundary else end)
        source = text[left:right]
        try:
            action = parse_action(source, applications=applications)
            route, reason = 'local', 'complete_registered_command'
        except AmbiguousTarget:
            action = None
            route, reason = 'clarify', 'ambiguous_registered_target'
        except UnknownCommand:
            try:
                action = parse_fuzzy_action(source, applications=applications)
                route, reason = 'local', 'fuzzy_registered_target'
            except AmbiguousTarget:
                action = None
                route, reason = 'clarify', 'ambiguous_registered_target'
            except UnknownCommand:
                action = None
                route, reason = (('clarify', 'unresolved_switch_target') if _unclear_switch_target(source)
                                 else ('contextual', 'unresolved_command_or_target'))
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
