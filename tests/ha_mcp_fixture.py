"""HA MCP wire fixtures: no model, device, microphone or installed HA access."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from mcp.types import CallToolResult, TextContent, Tool
import yaml

PREFIX = 'Live Context: An overview of the areas and the devices in this smart home:\n'
LIGHTS = [
    {'names': 'Bathroom', 'domain': 'light', 'areas': 'Bathroom', 'state': 'off'},
    {'names': 'Nanoleafs', 'domain': 'light', 'areas': 'Bedroom', 'state': 'off'},
    {'names': 'Table glow', 'domain': 'light', 'areas': 'Bedroom', 'state': 'off'},
    {'names': 'Under glow', 'domain': 'light', 'areas': 'Bedroom', 'state': 'off'},
]


def context(rows=None):
    return {'success': True, 'result': PREFIX + yaml.safe_dump(LIGHTS if rows is None else rows)}


def receipt(names=('Nanoleafs',), *, area=None):
    return {'speech': {}, 'response_type': 'action_done', 'data': {
        'success': ([{'name': area, 'type': 'area', 'id': area.casefold().replace(' ', '_')}] if area else [])
                   + [{'name': name, 'type': 'entity', 'id': 'light.' + name.casefold().replace(' ', '_')}
                    for name in names], 'failed': []}}


def result(document, *, error=False):
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(document))], isError=error)


def tools():
    props = {'name': {'type': 'string'}, 'area': {'type': 'string'},
             'domain': {'type': 'array', 'items': {'type': 'string'}}}
    return [Tool(name='homeassistant__GetLiveContext', inputSchema={
                'type': 'object', 'properties': {'domain': {'type': 'string'}}}),
            *[Tool(name='intent__HassTurn' + state,
                   inputSchema={'type': 'object', 'properties': props}) for state in ('On', 'Off')]]


class Peer:
    def __init__(self, document=None):
        self.tools, self.context = tools(), context()
        self.result = result(receipt(area='Bedroom') if document is None else document)
        self.calls, self.closed = [], False
        self.start_error = self.read_error = self.effect_error = self.close_error = None

    async def start(self):
        if self.start_error:
            raise self.start_error
        return self.tools

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name.endswith('GetLiveContext'):
            if self.read_error:
                raise self.read_error
            return result(self.context)
        if self.effect_error:
            raise self.effect_error
        return self.result

    @property
    def effects(self):
        return [call for call in self.calls if not call[0].endswith('GetLiveContext')]

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


@contextmanager
def serve(*, document=None, disconnect=False, auth_status=200):
    """Actual Streamable HTTP with the production MCPPeer; no fake HA REST API."""
    calls, auth = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def reply(self, status, value=None):
            body = json.dumps(value).encode() if value is not None else b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            self.reply(200, {})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            auth.append(self.headers.get('Authorization'))
            if auth_status != 200:
                self.reply(auth_status, {'error': 'fixture authorization rejected'})
                return
            if 'id' not in request:
                self.reply(202)
                return
            method = request['method']
            if method == 'initialize':
                value = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'fixture-ha', 'version': '1'}}
            elif method == 'tools/list':
                value = {'tools': [tool.model_dump(exclude_none=True) for tool in tools()]}
            elif method == 'tools/call':
                name, arguments = request['params']['name'], request['params']['arguments']
                calls.append((name, arguments))
                if disconnect and not name.endswith('GetLiveContext'):
                    self.connection.shutdown(2)
                    self.connection.close()
                    self.close_connection = True
                    return
                value = result(context() if name.endswith('GetLiveContext')
                               else receipt(area='Bedroom') if document is None else document).model_dump(exclude_none=True)
            else:
                self.reply(400)
                return
            self.reply(200, {'jsonrpc': '2.0', 'id': request['id'], 'result': value})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/mcp', calls, auth
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
