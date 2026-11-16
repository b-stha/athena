"""Validated, immutable application vocabulary supplied by Windows."""

from dataclasses import dataclass
import re
from types import MappingProxyType

from router import normalize_command


APPLICATION_ID = re.compile(r'app_[0-9a-f]{32}')
REVISION = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class Application:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True)
class ApplicationCatalog:
    revision: str
    applications: object
    vocabulary: object

    @classmethod
    def from_document(cls, document):
        if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
                or document['schema_version'] != 1
                or not isinstance(document.get('revision'), str)
                or not REVISION.fullmatch(document['revision'])
                or not isinstance(document.get('applications'), list)
                or len(document['applications']) > 2000):
            raise ValueError('Invalid Windows application catalog.')
        apps, names = {}, {}
        for row in document['applications']:
            if not isinstance(row, dict):
                raise ValueError('Invalid Windows application entry.')
            app_id, name = row.get('id'), row.get('display_name')
            aliases, operations = row.get('aliases'), row.get('operations')
            if (not isinstance(app_id, str) or not APPLICATION_ID.fullmatch(app_id) or app_id in apps
                    or not _name(name) or not isinstance(aliases, list) or len(aliases) > 8
                    or not all(_name(alias) for alias in aliases)
                    or not isinstance(operations, list) or not 1 <= len(operations) <= 8
                    or not all(isinstance(op, str) and re.fullmatch(r'[a-z_]{1,32}', op)
                               for op in operations)):
                raise ValueError('Invalid or duplicate Windows application entry.')
            app = Application(app_id, name, tuple(aliases), tuple(operations))
            apps[app_id] = app
            if not {'launch', 'close'}.intersection(operations):
                continue
            for alias in (name, *aliases):
                # Hyphenated Start-menu names also accept their spoken spacing.
                # Collisions remain multiple IDs and require clarification.
                spoken = alias.replace('-', ' ').replace('+', ' plus ').replace('&', ' and ')
                for variant in {alias, alias.replace('-', ' '), spoken}:
                    key = normalize_command(variant)
                    if key:
                        names.setdefault(key, set()).add(app_id)
        vocabulary = {name: tuple(sorted(ids)) for name, ids in names.items()}
        return cls(document['revision'], MappingProxyType(apps), MappingProxyType(vocabulary))


def _name(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 160
            and not any(ord(char) < 32 for char in value))
