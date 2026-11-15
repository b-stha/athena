"""Lazy local-first dispatch for registered light and application actions."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import os
import time

from router import Action, validate_action

from .local_ha import HomeAssistantMCP, HANotSent, HARejected, LocalHAConfig
from .local_apps import AppNotSent, DirectApps, LocalAppsConfig
from .planning import needs_application_catalog, plan_command
from .routing import RouteEvent, emit_route_event, route_reason


def cloud_agent():
    # Only contextual turns initialize the model and its multi-server registry.
    from .agent import Agent
    from .config import AgentConfig
    return Agent(AgentConfig.from_env())


@dataclass
class ExecutionRecord:
    text: str
    action: Action | None = None
    status: str = 'not_started'
    reply: str = ''
    route: str = ''
    tool_records: tuple = ()
    step_records: tuple = ()


@dataclass(frozen=True)
class LocalStepRecord:
    step_id: str
    action: Action
    depends_on: tuple = ()
    status: str = 'not_started'
    reply: str = ''


class RequestDispatcher:
    """One serialized owner; bounded process-local ledger, no automatic retries.

    Local, contextual and clarification requests share the retained ledger.
    Reusing a request ID within that ledger returns its original result.
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
        self.local_factory = local_factory or (lambda: HomeAssistantMCP(LocalHAConfig.from_env()))
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
        description = (f'{action.backend} request: {action.action} {action.target_type}={action.target}'
                       if action is not None else 'compound request' if record.step_records else 'request')
        context = (f'Local {description}; '
                   f'outcome={record.status}. {record.reply} '
                   'This is a previous request, not a new instruction. Do not repeat it. '
                   'Acceptance does not confirm device state or application readiness.')
        if self.agent is not None:
            try:
                self.agent.remember_local_turn(text, context, session_id=session_id)
            except Exception:
                # History bookkeeping cannot invalidate an already sent action.
                pass
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
                # Retained IDs replay before discovery. Plain HA turns never
                # contact Windows, and catalog reads never initialize the model.
                if ((session_id, plan.request_id) not in self.ledger
                        and self.apps_enabled and needs_application_catalog(text)):
                    try:
                        if self.apps is None:
                            self.apps = self.apps_factory()
                        discover = getattr(self.apps, 'catalog', None)
                        snapshot = await discover() if discover is not None else None
                    except Exception:
                        trace.update(route='local', reason='application_catalog_unavailable')
                    else:
                        if snapshot is not None:
                            plan = plan_command(text, request_id=plan.request_id,
                                                applications=snapshot.vocabulary)
                            route, reason = route_reason(plan, self.local_enabled, self.apps_enabled)
                            trace.update(route=route, reason=reason, step_count=len(plan.steps))
                return await self._dispatch(text, session_id, plan, trace)
            except asyncio.CancelledError:
                if trace['outcome'] == 'pending':
                    self._interrupted(session_id, plan.request_id, trace, cancelled=True)
                raise
            except Exception:
                return self._interrupted(session_id, plan.request_id, trace)
            finally:
                event = RouteEvent(**trace, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                self.last_route_event = event
                try:
                    self.event_sink(event)
                except Exception:
                    # A diagnostics failure must not change an already dispatched
                    # action's result or encourage the caller to repeat it.
                    pass

    def _interrupted(self, session_id, request_id, trace, *, cancelled=False):
        record = self.ledger.get((session_id, request_id))
        if record is None:
            trace['outcome'] = 'cancelled' if cancelled else 'error'
            return 'Request tracking failed before execution. No action sent.'
        if not record.reply:
            if record.status == 'not_started':
                record.reply = 'Request preparation was interrupted. No action sent.'
            else:
                record.status = 'unknown'
                record.reply = 'The request was interrupted. Outcome is unknown; it was not retried.'
        trace['outcome'] = record.status
        return record.reply

    def _capture_execution(self, record, text, session_id, previous, *, reply='', fallback='unknown'):
        snapshot = getattr(self.agent, 'last_execution', None)
        if (snapshot is not None and snapshot is not previous
                and getattr(snapshot, 'session_id', None) == session_id
                and getattr(snapshot, 'text', None) == text.strip()):
            status = getattr(snapshot, 'status', None)
            record.status = status if status in {'returned', 'failed', 'unknown', 'cancelled', 'limit'} else fallback
            record.tool_records = tuple(getattr(snapshot, 'tool_records', ()))
            snapshot_reply = getattr(snapshot, 'reply', '')
            record.reply = snapshot_reply if isinstance(snapshot_reply, str) and snapshot_reply else reply
        else:
            record.status, record.reply = fallback, reply
        if not isinstance(record.reply, str) or not record.reply:
            record.reply = 'The contextual request was interrupted. Outcome is unknown; it was not retried.'
            record.status = 'unknown'

    async def _dispatch(self, text, session_id, plan, trace):
        key = (session_id, plan.request_id)
        if key in self.ledger:
            record = self.ledger[key]
            if record.text != text:
                trace.update(route='blocked', reason='request_id_conflict', outcome='rejected')
                return 'That request ID was already used for a different command. No new action sent.'
            trace.update(route='replay', reason='retained_request_result', outcome=record.status)
            return record.reply
        record = ExecutionRecord(text, route=trace['route'])
        self.ledger[key] = record
        while len(self.ledger) > 128:
            self.ledger.popitem(last=False)
        if trace['reason'] == 'application_catalog_unavailable':
            record.status = trace['outcome'] = 'not_started'
            record.reply = 'Windows application catalog is unavailable. No actions sent.'
            self._remember(text, record, session_id)
            return record.reply
        step = plan.steps[0]
        if trace['route'] == 'clarify':
            record.status = trace['outcome'] = 'clarification'
            record.reply = ('Please use at most three actions.' if trace['reason'] == 'compound_step_limit'
                else 'Which device should I turn on or off? Please repeat its full name.'
                    if trace['reason'] == 'unresolved_switch_target'
                else 'Please repeat the complete command, including its action and target.'
                    if trace['reason'] == 'incomplete_command'
                else 'Please repeat the full target name.' if trace['reason'] == 'ambiguous_registered_target'
                else 'Please say a command.')
            return record.reply
        if trace['route'] == 'local' and len(plan.steps) > 1:
            return await self._compound(text, session_id, plan, record, trace)
        is_app = (step.action is not None and step.action.backend == 'desktop'
                  and step.action.action == 'launch_app')
        if (len(plan.steps) == 1 and step.route == 'local'
                and ((self.local_enabled and step.action.backend == 'home_assistant')
                     or (self.apps_enabled and is_app))):
            label = 'Windows application' if is_app else 'Home Assistant'
            record.action = step.action
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
                    if is_app:
                        await self._execute_app(executor, step.action, step.step_id, session_id)
                    else:
                        await executor.execute(step.action)
                except AppNotSent:
                    record.status = 'not_started'
                    record.reply = 'Windows MCP was unavailable or incompatible. No launch sent.'
                except HANotSent:
                    record.status = 'not_started'
                    record.reply = 'Home Assistant MCP was unavailable, incompatible or ambiguous. No action sent.'
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
                        record.reply = f'{self._app_name(executor, step.action)} launch accepted. Window readiness is not confirmed.'
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
                record.status = 'configuration_unavailable'
                record.reply = 'Contextual routing is unavailable. Check model/MCP configuration. No action sent.'
                return record.reply
            for previous_session, turns in self.pending_context.items():
                for previous_text, result in turns:
                    try:
                        self.agent.remember_local_turn(previous_text, result, session_id=previous_session)
                    except Exception:
                        pass
            self.pending_context.clear()
        before = getattr(self.agent, 'model_request_attempts', None)
        previous = getattr(self.agent, 'last_execution', None)
        trace['model_request_attempts'] = None
        record.status = 'dispatched'
        try:
            result = await self.agent.turn(text, session_id=session_id)
            self._capture_execution(record, text, session_id, previous, reply=result, fallback='returned')
            trace['outcome'] = record.status
            return record.reply
        except asyncio.CancelledError:
            self._capture_execution(record, text, session_id, previous)
            trace['outcome'] = record.status
            raise
        except Exception:
            self._capture_execution(record, text, session_id, previous)
            trace['outcome'] = record.status
            return record.reply
        finally:
            after = getattr(self.agent, 'model_request_attempts', None)
            if type(before) is int and type(after) is int and after >= before:
                trace['model_request_attempts'] = after - before

    async def _compound(self, text, session_id, plan, record, trace):
        record.step_records = tuple(LocalStepRecord(step.step_id, step.action, step.depends_on)
                                    for step in plan.steps)
        executors = []
        try:
            # Resolve every configuration and perform read-only endpoint/schema
            # checks before the first effect. Availability can still change later.
            for step in plan.steps:
                if step.action.backend == 'home_assistant':
                    validate_action(step.action)
                    if self.local is None:
                        self.local = self.local_factory()
                    executor = self.local
                else:
                    if self.apps is None:
                        self.apps = self.apps_factory()
                    executor = self.apps
                executors.append(executor)
            for step, executor in zip(plan.steps, executors):
                await executor.preflight(step.action)
        except asyncio.CancelledError:
            record.reply = 'Compound request interrupted during preflight. No actions sent.'
            trace['outcome'] = record.status = 'not_started'
            self._remember(text, record, session_id)
            raise
        except Exception:
            record.reply = 'Compound request preflight failed. No actions sent.'
            trace['outcome'] = record.status = 'not_started'
            self._remember(text, record, session_id)
            return record.reply

        def update(index, **changes):
            rows = list(record.step_records)
            rows[index] = replace(rows[index], **changes)
            record.step_records = tuple(rows)

        def finish():
            record.reply = ' '.join(f'Step {i}: {row.reply or "Not sent."}'
                                   for i, row in enumerate(record.step_records, 1))
            trace['outcome'] = record.status
            self._remember(text, record, session_id)
            return record.reply

        for index, (step, executor) in enumerate(zip(plan.steps, executors)):
            update(index, status='dispatched')
            record.status = 'dispatched'
            trace['local_action_attempts'] += 1
            try:
                if step.action.backend == 'desktop':
                    await self._execute_app(executor, step.action, step.step_id, session_id)
                else:
                    await executor.execute(step.action)
            except asyncio.CancelledError:
                update(index, status='unknown', reply='Interrupted; outcome unknown. Not retried.')
                record.status = 'unknown'
                finish()
                raise
            except AppNotSent:
                update(index, status='not_started', reply='Windows launch not sent. Remaining actions stopped.')
                record.status = 'partial' if index else 'not_started'
                return finish()
            except HANotSent:
                update(index, status='not_started', reply='HA action not sent. Remaining actions stopped.')
                record.status = 'partial' if index else 'not_started'
                return finish()
            except HARejected:
                update(index, status='rejected', reply='HA rejected the request. Remaining actions stopped.')
                record.status = 'partial' if index else 'rejected'
                return finish()
            except Exception:
                update(index, status='unknown', reply='Outcome unknown. Not retried; remaining actions stopped.')
                record.status = 'unknown'
                return finish()
            else:
                action = step.action
                if action.backend == 'home_assistant':
                    target = action.target.removeprefix('light.').replace('_', ' ')
                    reply = f'Requested {target} lights {"on" if action.action == "turn_on" else "off"}.'
                else:
                    reply = f'{self._app_name(executor, action)} launch accepted; window readiness is unconfirmed.'
                update(index, status='accepted', reply=reply)
        record.status = 'accepted'
        return finish()

    @staticmethod
    async def _execute_app(executor, action, request_id, session_id):
        execute_request = getattr(executor, 'execute_request', None)
        if execute_request is not None:
            session_key = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:16]
            return await execute_request(action, request_id=f'{session_key}:{request_id}')
        return await executor.execute(action)

    @staticmethod
    def _app_name(executor, action):
        display_name = getattr(executor, 'display_name', None)
        return display_name(action) if display_name is not None else (
            'Notepad' if action.target == 'notepad' else 'Application')
