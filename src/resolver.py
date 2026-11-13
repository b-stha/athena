"""Resolve transcribed text into commands while preserving exact actions."""

import re

from rapidfuzz.distance import Levenshtein


PUNCTUATION = str.maketrans({mark: " " for mark in ",.!?;:"})


def normalize(text):
    return " ".join(text.lower().translate(PUNCTUATION).split())


class AmbiguousTarget(ValueError):
    """Multiple registered targets are too close to distinguish."""


def resolve_target(name, candidates):
    """Correct only a complete target slot, never an action or extra clause."""
    if name in candidates:
        return candidates[name]
    if not re.fullmatch(r'[a-z]+(?: [a-z]+){0,2}', name):
        raise ValueError('Target must be a complete short name.')
    blocked = {'and', 'or', 'then', 'not', 'never', 'no', 'on', 'off', 'up', 'down',
               'my', 'the', 'a', 'an', 'new', 'latest', 'recent', 'previous', 'same', 'file', 'document',
               'please', 'if', 'unless', 'only', 'before', 'after', 'without'}
    if set(name.split()) & blocked and name != 'no pad':
        raise ValueError('Target contains unresolved scope or modifiers.')

    compact = name.replace(' ', '')
    distances = {}
    for candidate, canonical in candidates.items():
        candidate = candidate.replace(' ', '')
        distance = Levenshtein.distance(compact, candidate)
        score = Levenshtein.normalized_distance(compact, candidate)
        previous = distances.get(canonical, (float('inf'), float('inf')))
        distances[canonical] = min(previous, (score, distance))
    ranked = sorted((score, distance, target) for target, (score, distance) in distances.items())
    if not ranked:
        raise ValueError('No supported targets for this action.')
    score, distance, target = ranked[0]
    if distance and (len(compact) < 5 or distance > 2 or score > 0.30):
        raise ValueError('Target not recognized. Please repeat its name.')
    if len(ranked) > 1 and ranked[1][0] - score < 0.20:
        raise AmbiguousTarget('Target is ambiguous. Please repeat its full name.')
    return target


def resolve(transcript, targets):
    command = normalize(transcript)
    if command in {"", "exit", "quit"}:
        return command
    verb = next((v for v in sorted(targets, key=len, reverse=True)
                 if command.startswith(v + " ")), None)
    if verb is None:
        raise ValueError("Unrecognized action. Please repeat the command.")
    name = command[len(verb) + 1:]
    candidates = targets[verb]
    return f"{verb} {resolve_target(name, candidates)}"
