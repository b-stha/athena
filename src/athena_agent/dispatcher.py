"""Lazy local-first dispatch for registered light and application actions."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import os
import re
import time

from router import Action, validate_action

from .local_ha import HomeAssistantMCP, HANotSent, HARejected, LocalHAConfig
from .local_apps import AppExecutionResult, AppNeedsWindowChoice, AppNotSent, DirectApps, LocalAppsConfig
from .local_power import DirectPower, PowerExecutionResult, PowerNotSent, PowerRejected
from .planning import WAKE_PREFIX, needs_application_catalog, plan_command
from .routing import RouteEvent, emit_route_event, is_power_step, route_reason


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
                 apps_factory=None, apps_enabled=None, power_factory=None, power_enabled=None):
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
        self.power_enabled = apps_enabled if power_enabled is None else power_enabled
        if not isinstance(self.power_enabled, bool):
            raise ValueError('power_enabled must be boolean.')
        self.agent_factory = agent_factory or cloud_agent
        self.local_factory = local_factory or (lambda: HomeAssistantMCP(LocalHAConfig.from_env()))
        self.apps_factory = apps_factory or (lambda: DirectApps(LocalAppsConfig.from_env()))
        self.power_factory = power_factory or DirectPower
        self.power = None
        self.apps = None
        self.agent = self.local = None
        self.ledger = OrderedDict()
        self.pending_context = OrderedDict()
        self.window_choices = OrderedDict()
        self.lock = asyncio.Lock()
        self.event_sink = emit_route_event if event_sink is None else event_sink
        self.last_route_event = None

    async def close(self):
        try:
            if self.power is not None:
                await self.power.close()
        finally:
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
            route, reason = route_reason(plan, self.local_enabled, self.apps_enabled, self.power_enabled)
            trace = {'request_id': plan.request_id, 'route': route, 'reason': reason,
                     'outcome': 'pending', 'step_count': len(plan.steps),
                     'model_request_attempts': 0, 'local_action_attempts': 0}
            try:
                # Retained IDs replay before discovery. Plain HA turns never
                # contact Windows, and catalog reads never initialize the model.
                if ((session_id, plan.request_id) not in self.ledger
                        and trace['reason'] not in {'power_must_be_last', 'mixed_power_not_integrated'}
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
                            route, reason = route_reason(plan, self.local_enabled, self.apps_enabled, self.power_enabled)
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
        selected_window_id = None
        selection = re.fullmatch(r'(?:please )?(?:(?:close|select|choose) )?window '
                                 r'(one|two|three|four|five|six|seven|eight|[1-9][0-9]?)'
                                 r'(?: please)?[.!]?',
                                 WAKE_PREFIX.sub('', text, count=1).strip(), re.IGNORECASE)
        pending = self.window_choices.pop(session_id, None)
        if selection is not None:
            if pending is None or pending[0] <= time.monotonic():
                record.status = trace['outcome'] = 'clarification'
                trace.update(route='clarify', reason='window_selection_expired')
                record.reply = 'No current window selection. Please repeat the close command with the application name.'
                return record.reply
            word = selection[1].casefold()
            index = int(word) if word.isdigit() else (
                'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight').index(word) + 1
            if index > len(pending[3]):
                self.window_choices[session_id] = pending
                record.status = trace['outcome'] = 'clarification'
                trace.update(route='clarify', reason='invalid_window_selection')
                record.reply = f'Please choose a window from 1 to {len(pending[3])}.'
                return record.reply
            if self.apps is None or self.apps.snapshot is None or self.apps.snapshot.revision != pending[2]:
                record.status = trace['outcome'] = 'not_started'
                trace.update(route='local', reason='window_catalog_changed')
                record.reply = 'The application catalog changed. Please repeat the close command. No action sent.'
                return record.reply
            selected_window_id = pending[3][index - 1][0]
            step = replace(plan.steps[0], route='local', action=pending[1], reason='explicit_window_selection')
            plan = replace(plan, steps=(step,), scope_strategy='window_selection')
            trace.update(route='local', reason='explicit_window_selection', step_count=1)
            record.route = 'local'
        if trace['reason'] == 'application_catalog_unavailable':
            record.status = trace['outcome'] = 'not_started'
            record.reply = 'Windows application catalog is unavailable. No actions sent.'
            self._remember(text, record, session_id)
            return record.reply
        step = plan.steps[0]
        if trace['route'] == 'clarify':
            record.status = trace['outcome'] = 'clarification'
            record.reply = ('Please use at most three actions.' if trace['reason'] == 'compound_step_limit'
                else 'Please put the PC power command last and use only one per request.'
                    if trace['reason'] == 'power_must_be_last'
                else 'Please issue PC power commands separately from contextual requests.'
                    if trace['reason'] == 'mixed_power_not_integrated'
                else 'Please issue the close command separately from contextual actions for now.'
                    if trace['reason'] == 'mixed_close_not_integrated'
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
                  and step.action.action in {'launch_app', 'close_app'})
        is_power = is_power_step(step)
        if (len(plan.steps) == 1 and step.route == 'local'
                and ((self.local_enabled and step.action.backend == 'home_assistant')
                     or (self.apps_enabled and is_app)
                     or (self.power_enabled and is_power))):
            label = 'PC power' if is_power else 'Windows application' if is_app else 'Home Assistant'
            record.action = step.action
            try:
                if is_power:
                    if self.power is None:
                        self.power = self.power_factory()
                    executor = self.power
                elif is_app:
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
                    if is_app or is_power:
                        result = await self._execute_desktop(executor, step.action, step.step_id, session_id,
                                                         window_id=selected_window_id)
                    else:
                        await executor.execute(step.action)
                except PowerNotSent:
                    record.status = 'not_started'
                    record.reply = 'PC power request was not sent. Check the Windows MCP connection or Wake-on-LAN configuration.'
                except PowerRejected:
                    record.status = 'rejected'
                    record.reply = 'Windows rejected the PC power request. It was not retried.'
                except AppNotSent:
                    record.status = 'not_started'
                    operation = 'close' if step.action.action == 'close_app' else 'launch'
                    record.reply = f'Windows MCP was unavailable or incompatible. No {operation} sent.'
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
                    if is_power:
                        record.status, record.reply = self._power_reply(step.action, result)
                    elif is_app:
                        record.status, record.reply = self._app_reply(executor, step.action, result)
                        if (step.action.action == 'close_app' and isinstance(result, AppExecutionResult)
                                and result.status == 'clarification' and 1 < len(result.choices) <= 8):
                            self.window_choices[session_id] = (time.monotonic() + 50, step.action,
                                executor.snapshot.revision, result.choices)
                            while len(self.window_choices) > 8:
                                self.window_choices.popitem(last=False)
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
                if is_power_step(step):
                    if self.power is None:
                        self.power = self.power_factory()
                    executor = self.power
                elif step.action.backend == 'home_assistant':
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
        except AppNeedsWindowChoice:
            record.reply = 'The close needs a window selection. Please issue that close command separately. No actions sent.'
            trace['outcome'] = record.status = 'clarification'
            self._remember(text, record, session_id)
            return record.reply
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
                    result = await self._execute_desktop(executor, step.action, step.step_id, session_id)
                else:
                    await executor.execute(step.action)
            except asyncio.CancelledError:
                update(index, status='unknown', reply='Interrupted; outcome unknown. Not retried.')
                record.status = 'unknown'
                finish()
                raise
            except PowerNotSent:
                update(index, status='not_started', reply='PC power request not sent. Remaining actions stopped.')
                record.status = 'partial' if index else 'not_started'
                return finish()
            except PowerRejected:
                update(index, status='rejected', reply='Windows rejected the power request. Not retried.')
                record.status = 'partial' if index else 'rejected'
                return finish()
            except AppNotSent:
                update(index, status='not_started', reply='Windows action not sent. Remaining actions stopped.')
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
                    status, reply = (self._power_reply(action, result) if is_power_step(step)
                                     else self._app_reply(executor, action, result))
                    if status not in {'accepted', 'closed', 'hidden'}:
                        update(index, status=status, reply=reply + ' Remaining actions stopped.')
                        record.status = 'partial' if index else status
                        return finish()
                    update(index, status=status, reply=reply)
                    continue
                update(index, status='accepted', reply=reply)
        record.status = 'accepted'
        return finish()

    @staticmethod
    async def _execute_desktop(executor, action, request_id, session_id, *, window_id=None):
        execute_request = getattr(executor, 'execute_request', None)
        if execute_request is not None:
            session_key = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:16]
            kwargs = {'request_id': f'{session_key}:{request_id}'}
            if window_id is not None:
                kwargs['window_id'] = window_id
            return await execute_request(action, **kwargs)
        return await executor.execute(action)

    @staticmethod
    def _power_reply(action, result):
        if not isinstance(result, PowerExecutionResult):
            return 'unknown', 'PC power outcome is unknown. It was not retried.'
        if action.action == 'wake' and result.reason == 'wake_packet_sent':
            return 'accepted', 'Wake packet sent. PC startup is not confirmed.'
        if result.status == 'scheduled':
            return 'accepted', (f'PC {action.action} scheduled in {result.delay_seconds} seconds. '
                                'Completion is not confirmed; unsaved work may require attention.')
        if result.status == 'submitted':
            return 'accepted', f'PC {action.action} requested. Completion is not confirmed.'
        return 'unknown', 'PC power outcome is unknown. It was not retried.'

    @staticmethod
    def _app_reply(executor, action, result):
        name = RequestDispatcher._app_name(executor, action)
        if action.action == 'launch_app':
            return 'accepted', f'{name} launch accepted. Window readiness is not confirmed.'
        if not isinstance(result, AppExecutionResult):
            return 'unknown', 'Windows did not return a verified close receipt. Not retried.'
        if result.status == 'closed':
            return 'closed', f'{name} window closed.'
        if result.status == 'hidden':
            return 'hidden', f'{name} window hidden. The application may still be running.'
        if result.status == 'pending':
            return 'pending', f'Close requested for {name}, but its window is still open. Check the app for unsaved work or confirmation. Not retried.'
        if result.status == 'clarification':
            if len(result.choices) > 8:
                return 'clarification', f'Too many {name} windows to select here. Please close some manually.'
            choices = ' '.join(f'{index}: {title or "Untitled window"}.'
                               for index, (_, title) in enumerate(result.choices, 1))
            return 'clarification', f'Multiple {name} windows. {choices} Say "close window one", or another listed number.'
        reasons = {'window_not_identified': f'No matching {name} window could be identified.',
                   'close_not_sent': f'{name} could not receive a normal close request. Check for a dialog or permissions.',
                   'window_changed': 'The selected window changed.',
                   'selection_expired': 'The window selection expired.',
                   'catalog_changed': 'The application catalog changed.'}
        return 'not_started', reasons.get(result.reason, 'Windows could not prepare the close request.') + ' No close sent.'

    @staticmethod
    def _app_name(executor, action):
        display_name = getattr(executor, 'display_name', None)
        return display_name(action) if display_name is not None else (
            'Notepad' if action.target == 'notepad' else 'Application')
