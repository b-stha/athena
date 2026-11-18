"""Fixed PC power operations over Windows MCP; Wake-on-LAN stays on the Pi."""

import asyncio
from dataclasses import dataclass

from integrations.desktop import prepare_wake_packet, wake_pc
from router import validate_action
from .config import load_server_config
from .local_apps import _check_tool, _document
from .tools import MCPPeer


class PowerNotSent(RuntimeError):
    pass


class PowerRejected(RuntimeError):
    pass


class PowerUnknownOutcome(RuntimeError):
    pass


@dataclass(frozen=True)
class PowerExecutionResult:
    status: str
    reason: str
    delay_seconds: int = 0


class DirectPower:
    def __init__(self, server=None, *, peer_factory=MCPPeer, waker=wake_pc,
                 wake_config=prepare_wake_packet):
        self.server = server
        self.peer_factory, self.waker, self.wake_config = peer_factory, waker, wake_config

    @staticmethod
    def _validate(action):
        validate_action(action)
        if (action.backend != 'desktop' or action.target != 'pc'
                or action.target_type != 'device'
                or action.action not in {'wake', 'shutdown', 'restart', 'sleep'}):
            raise ValueError('Use a registered PC power operation.')

    def _peer(self):
        if self.server is None:
            self.server = load_server_config('windows')
        return self.peer_factory(self.server, 15, 5)

    async def close(self):
        pass

    async def _ready(self, peer, action, request_id):
        tools = await peer.start()
        _check_tool(tools, 'AthenaPowerStatus', {})
        _check_tool(tools, 'AthenaPowerAction', {'action': action, 'request_id': request_id})
        result = await peer.call('AthenaPowerStatus', {})
        document = _document(result)
        actions = document.get('actions')
        if (result.isError or type(document.get('schema_version')) is not int
                or document['schema_version'] != 1 or document.get('status') != 'ready'
                or document.get('state_confirmed') is not False
                or not isinstance(actions, list)
                or not all(isinstance(item, str) for item in actions)
                or len(actions) != len(set(actions))
                or not set(actions) <= {'shutdown', 'restart', 'sleep'}
                or action not in actions or type(document.get('delay_seconds')) is not int
                or not 1 <= document['delay_seconds'] <= 10):
            raise PowerNotSent('Windows power capability is unavailable or incompatible.')

    async def preflight(self, action):
        self._validate(action)
        if action.action == 'wake':
            try:
                self.wake_config()
            except Exception:
                raise PowerNotSent('Wake-on-LAN configuration is unavailable.') from None
            return
        peer = None
        try:
            peer = self._peer()
            await self._ready(peer, action.action, 'schema_check')
        except Exception:
            raise PowerNotSent('Windows power tool is unavailable or incompatible.') from None
        finally:
            if peer is not None:
                await peer.close()

    async def execute_request(self, action, *, request_id):
        self._validate(action)
        if action.action == 'wake':
            await self.preflight(action)
            try:
                await asyncio.to_thread(self.waker)
            except Exception:
                raise PowerUnknownOutcome('Wake packet outcome is unknown; not retried.') from None
            return PowerExecutionResult('submitted', 'wake_packet_sent')
        peer = None
        arguments = {'action': action.action, 'request_id': request_id}
        try:
            try:
                peer = self._peer()
                await self._ready(peer, action.action, request_id)
            except Exception:
                raise PowerNotSent('Windows power tool is unavailable or incompatible.') from None
            try:
                result = await peer.call('AthenaPowerAction', arguments)
                document = _document(result)
                if (type(document.get('schema_version')) is not int or document['schema_version'] != 1
                        or document.get('operation') != 'power'
                        or any(document.get(key) != value for key, value in arguments.items())
                        or document.get('state_confirmed') is not False
                        or type(document.get('delay_seconds')) is not int
                        or not 1 <= document['delay_seconds'] <= 10
                        or not isinstance(document.get('reason'), str)
                        or not 0 < len(document['reason']) <= 80):
                    raise ValueError('Invalid power receipt.')
            except Exception:
                raise PowerUnknownOutcome('Windows power outcome is unknown; not retried.') from None
            status = document.get('status')
            if status == 'not_started':
                raise PowerNotSent('Windows did not schedule the power request.')
            if status == 'rejected':
                raise PowerRejected('Windows rejected the power request.')
            if status not in {'scheduled', 'submitted'} or result.isError:
                raise PowerUnknownOutcome('Windows power outcome is unknown; not retried.')
            return PowerExecutionResult(status, document['reason'], document['delay_seconds'])
        finally:
            if peer is not None:
                await peer.close()
