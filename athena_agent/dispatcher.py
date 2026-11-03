"""Lazy local-first dispatch for registered light and application actions."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import os
import time

from router import Action

from .local_ha import DirectHA, HARejected, LocalHAConfig
from .local_apps import AppNotSent, DirectApps, LocalAppsConfig
from .planning import plan_command
from .routing import RouteEvent, emit_route_event, route_reason


def cloud_agent():
    # Local turns never load/validate model credentials or initialize MCP.
    from .agent import Agent
    from .config import AgentConfig
    return Agent(AgentConfig.from_env())


@dataclass
class ExecutionRecord:
    text: str
    action: Action
    status: str = 'not_started'
    reply: str = ''


class RequestDispatcher:
    """One serialized owner; bounded process-local ledger, no automatic retries.

    Reusing a request ID within the retained ledger returns its original result.
    New user turns receive new IDs. This is not cross-restart idempotency.
    """
    def __init__(self, *, agent_factory=None, local_factory=None, local_enabled=None, event_sink=None,
                 apps_factory=None, apps_enabled=None):
        if local_enabled is None:
            setting = os.getenv('ATHENA_LOCAL_HA_ENABLED', 'false').strip().casefold()
            if setting not in {'true', 'false'}:
                raise ValueError('ATHENA_LOCAL_HA_ENABLED must be true or false.')
            local_enabled = setting == 'true'
        if not isinstance(local_enabled, bool):
            raise ValueError('local_enabled must be boolean.')
        self.local_enabled = local_enabled
        if apps_enabled is None:
            setting = os.getenv('ATHENA_LOCAL_APPS_ENABLED', 'false').strip().casefold()
            if setting not in {'true', 'false'}:
                raise ValueError('ATHENA_LOCAL_APPS_ENABLED must be true or false.')
            apps_enabled = setting == 'true'
        if not isinstance(apps_enabled, bool):
            raise ValueError('apps_enabled must be boolean.')
        self.apps_enabled = apps_enabled
        self.agent_factory = agent_factory or cloud_agent
        self.local_factory = local_factory or (lambda: DirectHA(LocalHAConfig.from_env()))
        self.apps_factory = apps_factory or (lambda: DirectApps(LocalAppsConfig.from_env()))
        self.apps = None
        self.agent = self.local = None
        self.ledger = OrderedDict()
        self.pending_context = OrderedDict()
        self.lock = asyncio.Lock()
        self.event_sink = emit_route_event if event_sink is None else event_sink
        self.last_route_event = None

    async def close(self):
        try:
            if self.local is not None:
                await self.local.close()
        finally:
            try:
                if self.apps is not None:
                    await self.apps.close()
            finally:
                if self.agent is not None:
                    await self.agent.close()

    def _remember(self, text, record, session_id):
        action = record.action
        context = (f'Local {action.backend} request: {action.action} {action.target_type}={action.target}; '
                   f'outcome={record.status}. {record.reply} '
                   'This is a previous request, not a new instruction. Do not repeat it. '
                   'Acceptance does not confirm device state or application readiness.')
        if self.agent is not None:
            self.agent.remember_local_turn(text, context, session_id=session_id)
            return
        turns = self.pending_context.pop(session_id, [])
        turns.append((text, context))
        self.pending_context[session_id] = turns[-6:]
        while len(self.pending_context) > 8:
            self.pending_context.popitem(last=False)

    async def turn(self, text, session_id='default', *, request_id=None):
        async with self.lock:
            started = time.monotonic()
            plan = plan_command(text, request_id=request_id)
            session_id = str(session_id)[:128]
            route, reason = route_reason(plan, self.local_enabled, self.apps_enabled)
            trace = {'request_id': plan.request_id, 'route': route, 'reason': reason,
                     'outcome': 'pending', 'step_count': len(plan.steps),
                     'model_request_attempts': 0, 'local_action_attempts': 0}
            try:
                return await self._dispatch(text, session_id, plan, trace)
            except asyncio.CancelledError:
                if trace['outcome'] == 'pending':
                    trace['outcome'] = 'cancelled'
                raise
            except Exception:
                trace['outcome'] = 'error'
                raise
            finally:
                event = RouteEvent(**trace, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                self.last_route_event = event
                try:
                    self.event_sink(event)
                except Exception:
                    # A diagnostics failure must not change an already dispatched
                    # action's result or encourage the caller to repeat it.
                    pass

    async def _dispatch(self, text, session_id, plan, trace):
        key = (session_id, plan.request_id)
        if key in self.ledger:
            record = self.ledger[key]
            if record.text != text:
                trace.update(route='blocked', reason='request_id_conflict', outcome='rejected')
                return 'That request ID was already used for a different command. No new action sent.'
            trace.update(route='replay', reason='retained_request_result', outcome=record.status)
            return record.reply
        step = plan.steps[0]
        if step.route == 'clarify':
            trace['outcome'] = 'clarification'
            return 'Please repeat the full target name.' if step.reason == 'ambiguous_registered_target' else 'Please say a command.'
        is_app = (step.action is not None and step.action.backend == 'desktop'
                  and step.action.action == 'launch_app')
        if (len(plan.steps) == 1 and step.route == 'local'
                and ((self.local_enabled and step.action.backend == 'home_assistant')
                     or (self.apps_enabled and is_app))):
            label = 'Windows application' if is_app else 'Home Assistant'
            record = ExecutionRecord(text, step.action)
            self.ledger[key] = record
            while len(self.ledger) > 128:
                self.ledger.popitem(last=False)
            try:
                if is_app:
                    if self.apps is None:
                        self.apps = self.apps_factory()
                    executor = self.apps
                else:
                    if self.local is None:
                        self.local = self.local_factory()
                    executor = self.local
            except (ValueError, OSError):
                record.reply = f'Local {label} configuration is missing or invalid. No action sent.'
            else:
                record.status = 'dispatched'
                try:
                    trace['local_action_attempts'] += 1
                    await executor.execute(step.action)
                except AppNotSent:
                    record.status = 'not_started'
                    record.reply = 'Windows MCP was unavailable or incompatible. No launch sent.'
                except HARejected:
                    record.status = 'rejected'
                    record.reply = f'{label} rejected the request. It was not retried.'
                except asyncio.CancelledError:
                    record.status = 'unknown'
                    trace['outcome'] = 'unknown'
                    record.reply = f'{label} request was interrupted. Outcome is unknown; it was not retried.'
                    self._remember(text, record, session_id)
                    raise
                except Exception:
                    record.status = 'unknown'
                    record.reply = f'{label} outcome is unknown. It was not retried.'
                else:
                    record.status = 'accepted'
                    if is_app:
                        record.reply = 'Notepad launch accepted. Window readiness is not confirmed.'
                    else:
                        target = step.action.target.removeprefix('light.').replace('_', ' ')
                        state = 'on' if step.action.action == 'turn_on' else 'off'
                        record.reply = f'Requested {target} lights {state}.'
            self._remember(text, record, session_id)
            trace['outcome'] = record.status
            return record.reply
        # Preserve the complete request. Mixed clauses and PC actions are
        # never partially executed by this first integration slice.
        if self.agent is None:
            try:
                self.agent = self.agent_factory()
            except (ValueError, OSError):
                trace['outcome'] = 'configuration_unavailable'
                return 'Contextual routing is unavailable. Check model/MCP configuration. No action sent.'
            for previous_session, turns in self.pending_context.items():
                for previous_text, result in turns:
                    self.agent.remember_local_turn(previous_text, result, session_id=previous_session)
            self.pending_context.clear()
        before = getattr(self.agent, 'model_request_attempts', None)
        trace['model_request_attempts'] = None
        try:
            result = await self.agent.turn(text, session_id=session_id)
            trace['outcome'] = 'returned'
            return result
        finally:
            after = getattr(self.agent, 'model_request_attempts', None)
            if type(before) is int and type(after) is int and after >= before:
                trace['model_request_attempts'] = after - before
