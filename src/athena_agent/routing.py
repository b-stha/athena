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


MAX_COMPOUND_STEPS = 3


def is_power_step(step):
    action = step.action
    return (action is not None and action.backend == 'desktop' and action.target == 'pc'
            and action.target_type == 'device'
            and action.action in {'wake', 'shutdown', 'restart', 'sleep'})


def supported_local_step(step, local_enabled, apps_enabled, power_enabled=None):
    power_enabled = apps_enabled if power_enabled is None else power_enabled
    return (step.route == 'local' and step.action is not None and
            ((local_enabled and step.action.backend == 'home_assistant') or
             (power_enabled and is_power_step(step)) or
             (apps_enabled and step.action.backend == 'desktop' and step.action.action in {'launch_app', 'close_app'})))


def route_reason(plan, local_enabled, apps_enabled=False, power_enabled=None):
    """Explain dispatcher policy separately from the planner's candidate labels."""
    step = plan.steps[0]
    power_enabled = apps_enabled if power_enabled is None else power_enabled
    power_steps = [index for index, item in enumerate(plan.steps) if is_power_step(item)]
    if power_enabled and len(plan.steps) > 1 and power_steps:
        if any(item.route != 'local' for item in plan.steps):
            return 'clarify', 'mixed_power_not_integrated'
        if len(power_steps) != 1 or power_steps[0] != len(plan.steps) - 1:
            return 'clarify', 'power_must_be_last'
    # Mixed closing is deferred until semantic mixed-plan execution is added.
    # This also covers a whole contextual request with sentence/scope guards.
    from .planning import BOUNDARY, is_app_close_request
    if (any(item.route == 'contextual' for item in plan.steps)
            and is_app_close_request(plan.original) and BOUNDARY.search(plan.original)):
        return 'clarify', 'mixed_close_not_integrated'
    for candidate in plan.steps:
        if candidate.route == 'clarify':
            return 'clarify', candidate.reason
    if len(plan.steps) > 1:
        if all(supported_local_step(item, local_enabled, apps_enabled, power_enabled) for item in plan.steps):
            if len(plan.steps) > MAX_COMPOUND_STEPS:
                return 'clarify', 'compound_step_limit'
            return 'local', 'complete_registered_compound'
        if any(is_app_close_request(item.text) for item in plan.steps):
            return 'clarify', 'mixed_close_not_integrated'
        return 'cloud', 'compound_execution_not_integrated'
    if step.route == 'local':
        fuzzy = step.reason == 'fuzzy_registered_target'
        if is_power_step(step) and power_enabled:
            return 'local', 'complete_registered_power_command'
        if step.action.backend == 'desktop' and step.action.action in {'launch_app', 'close_app'} and apps_enabled:
            return 'local', 'fuzzy_registered_application_target' if fuzzy else 'complete_registered_application_command'
        if step.action.backend != 'home_assistant':
            return 'cloud', 'local_backend_not_integrated'
        return ('local', 'fuzzy_registered_ha_target' if fuzzy else 'complete_registered_ha_command') if local_enabled else ('cloud', 'local_ha_disabled')
    return 'cloud', step.reason
