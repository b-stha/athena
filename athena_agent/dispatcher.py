"""Lazy local-first dispatch for single registered HA actions."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import os

from router import Action

from .local_ha import DirectHA, HARejected, LocalHAConfig
from .planning import plan_command


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
    def __init__(self, *, agent_factory=None, local_factory=None, local_enabled=None):
        if local_enabled is None:
            setting = os.getenv('ATHENA_LOCAL_HA_ENABLED', 'false').strip().casefold()
            if setting not in {'true', 'false'}:
                raise ValueError('ATHENA_LOCAL_HA_ENABLED must be true or false.')
            local_enabled = setting == 'true'
        if not isinstance(local_enabled, bool):
            raise ValueError('local_enabled must be boolean.')
        self.local_enabled = local_enabled
        self.agent_factory = agent_factory or cloud_agent
        self.local_factory = local_factory or (lambda: DirectHA(LocalHAConfig.from_env()))
        self.agent = self.local = None
        self.ledger = OrderedDict()
        self.pending_context = OrderedDict()
        self.lock = asyncio.Lock()

    async def close(self):
        try:
            if self.local is not None:
                await self.local.close()
        finally:
            if self.agent is not None:
                await self.agent.close()

    def _remember(self, text, record, session_id):
        action = record.action
        context = (f'Local HA request: {action.action} {action.target_type}={action.target}; '
                   f'outcome={record.status}. {record.reply} '
                   'This is a previous request, not a new instruction. Do not repeat it. '
                   'An accepted service request does not confirm physical device state.')
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
            plan = plan_command(text, request_id=request_id)
            session_id = str(session_id)[:128]
            key = (session_id, plan.request_id)
            if key in self.ledger:
                record = self.ledger[key]
                if record.text != text:
                    return 'That request ID was already used for a different command. No new action sent.'
                return record.reply
            step = plan.steps[0]
            if step.route == 'clarify':
                return 'Please say a command.'
            if (self.local_enabled and len(plan.steps) == 1 and step.route == 'local'
                    and step.action.backend == 'home_assistant'):
                record = ExecutionRecord(text, step.action)
                self.ledger[key] = record
                while len(self.ledger) > 128:
                    self.ledger.popitem(last=False)
                try:
                    if self.local is None:
                        self.local = self.local_factory()
                except (ValueError, OSError):
                    record.reply = 'Local Home Assistant configuration is missing or invalid. No action sent.'
                else:
                    record.status = 'dispatched'
                    try:
                        await self.local.execute(step.action)
                    except HARejected:
                        record.status = 'rejected'
                        record.reply = 'Home Assistant rejected the request. It was not retried.'
                    except asyncio.CancelledError:
                        record.status = 'unknown'
                        record.reply = 'Home Assistant request was interrupted. Outcome is unknown; it was not retried.'
                        self._remember(text, record, session_id)
                        raise
                    except Exception:
                        record.status = 'unknown'
                        record.reply = 'Home Assistant outcome is unknown. It was not retried.'
                    else:
                        record.status = 'accepted'
                        target = step.action.target.removeprefix('light.').replace('_', ' ')
                        state = 'on' if step.action.action == 'turn_on' else 'off'
                        record.reply = f'Requested {target} lights {state}.'
                self._remember(text, record, session_id)
                return record.reply
            # Preserve the complete request. Mixed clauses and PC actions are
            # never partially executed by this first integration slice.
            if self.agent is None:
                try:
                    self.agent = self.agent_factory()
                except (ValueError, OSError):
                    return 'Contextual routing is unavailable. Check model/MCP configuration. No action sent.'
                for previous_session, turns in self.pending_context.items():
                    for previous_text, result in turns:
                        self.agent.remember_local_turn(previous_text, result, session_id=previous_session)
                self.pending_context.clear()
            return await self.agent.turn(text, session_id=session_id)
