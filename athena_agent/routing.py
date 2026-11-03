"""Content-free routing diagnostics; collection is opt-in."""

from dataclasses import asdict, dataclass
import json
import os
import sys


@dataclass(frozen=True)
class RouteEvent:
    request_id: str
    route: str
    reason: str
    outcome: str
    step_count: int
    model_request_attempts: int | None
    local_action_attempts: int
    elapsed_ms: float
    schema_version: int = 1


def emit_route_event(event):
    if os.getenv('ATHENA_ROUTE_LOG', 'false').strip().casefold() == 'true':
        print('[athena.route] ' + json.dumps(asdict(event), separators=(',', ':')),
              file=sys.stderr, flush=True)


def route_reason(plan, local_enabled, apps_enabled=False):
    """Explain dispatcher policy separately from the planner's candidate labels."""
    step = plan.steps[0]
    if step.route == 'clarify':
        return 'clarify', step.reason
    if len(plan.steps) > 1:
        return 'cloud', 'compound_execution_not_integrated'
    if step.route == 'local':
        fuzzy = step.reason == 'fuzzy_registered_target'
        if step.action.backend == 'desktop' and step.action.action == 'launch_app' and apps_enabled:
            return 'local', 'fuzzy_registered_application_target' if fuzzy else 'complete_registered_application_command'
        if step.action.backend != 'home_assistant':
            return 'cloud', 'local_backend_not_integrated'
        return ('local', 'fuzzy_registered_ha_target' if fuzzy else 'complete_registered_ha_command') if local_enabled else ('cloud', 'local_ha_disabled')
    return 'cloud', step.reason
