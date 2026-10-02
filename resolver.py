"""Resolve transcribed text into commands while preserving exact actions."""

from rapidfuzz.distance import Levenshtein


PUNCTUATION = str.maketrans({mark: " " for mark in ",.!?;:"})


def normalize(text):
    return " ".join(text.lower().translate(PUNCTUATION).split())


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
    if name in candidates:
        return f"{verb} {candidates[name]}"

    compact = name.replace(" ", "")
    # Group aliases by canonical target so synonyms cannot create false ambiguity.
    distances = {}
    for candidate, canonical in candidates.items():
        candidate = candidate.replace(" ", "")
        distance = Levenshtein.distance(compact, candidate)
        score = Levenshtein.normalized_distance(compact, candidate)
        previous = distances.get(canonical, (float("inf"), float("inf")))
        distances[canonical] = min(previous, (score, distance))
    ranked = sorted((score, distance, name) for name, (score, distance) in distances.items())
    if not ranked:
        raise ValueError("No supported targets for this action.")
    score, distance, target = ranked[0]
    if distance and (len(compact) < 5 or distance > 5 or score > 0.65):
        raise ValueError("Target not recognized. Please repeat its name.")
    if len(ranked) > 1 and ranked[1][0] - score < 0.15:
        raise ValueError("Target is ambiguous. Please repeat its full name.")
    return f"{verb} {target}"
