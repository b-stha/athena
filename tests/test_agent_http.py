"""Real HTTP integration with loopback cloud and MCP fakes; no live device actions."""

from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack, contextmanager
import importlib.metadata
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import unittest

PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3V8AAAAASUVORK5CYII="
base64.b64decode(PIXEL, validate=True)


class State:
    def __init__(self, *, timeout_windows=False):
        self.timeout_windows = timeout_windows
        self.lock = threading.Lock()
        self.auth_failures = []
        self.discoveries = []
        self.calls = []
        self.cloud_requests = []
        self.cloud_errors = []


@contextmanager
def serve(kind, state):
    token = f"{kind}-test-token"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                # A deliberately timed-out client may abort its keep-alive socket.
                pass

        def reply(self, status, body=None):
            encoded = json.dumps(body).encode() if body is not None else b""
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except ConnectionError:
                pass

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            self.reply(200, {})

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            request = json.loads(data)
            if self.headers.get("Authorization") != f"Bearer {token}":
                with state.lock:
                    state.auth_failures.append(kind)
                self.reply(401, {"error": "invalid test credential"})
                return
            try:
                if kind == "cloud":
                    self.cloud(request)
                else:
                    self.mcp(request)
            except Exception as error:
                with state.lock:
                    state.cloud_errors.append(f"{kind}: {type(error).__name__}: {error}")
                self.reply(500, {"error": str(error)})

        def mcp(self, request):
            method = request["method"]
            params = request.get("params", {})
            if "id" not in request:
                self.reply(202)
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": f"fake-{kind}", "version": "1.0"},
                }
            elif method == "tools/list":
                cursor = params.get("cursor")
                with state.lock:
                    state.discoveries.append((kind, cursor))
                if cursor is None:
                    result = {
                        "tools": [{
                            "name": "shared_action",
                            "description": f"Independent smoke {kind} action",
                            "inputSchema": {
                                "type": "object", "properties": {"target": {"type": "string"}},
                                "required": ["target"], "additionalProperties": False,
                            },
                        }],
                        "nextCursor": "second-page",
                    }
                else:
                    assert cursor == "second-page", cursor
                    result = {"tools": [{
                        "name": "state.with-unsafe:name-and-" + "x" * 80,
                        "description": f"Independent smoke {kind} paginated state",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    }]}
            elif method == "tools/call":
                assert params["name"] == "shared_action", params
                expected = "desk" if kind == "ha" else "desktop"
                assert params.get("arguments") == {"target": expected}, params
                with state.lock:
                    state.calls.append((kind, params["name"], params["arguments"]))
                if kind == "windows" and state.timeout_windows:
                    time.sleep(0.6)
                content = [{"type": "text", "text": "light_on" if kind == "ha" else "desktop_observed"}]
                if kind == "windows":
                    content.append({"type": "image", "data": PIXEL, "mimeType": "image/png"})
                result = {"content": content, "isError": False}
            else:
                self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": method}})
                return
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})

        def cloud(self, request):
            assert self.path == "/v1/chat/completions", self.path
            assert request["model"] == "fake-vision-model", request.get("model")
            with state.lock:
                number = len(state.cloud_requests)
                state.cloud_requests.append(request)
            messages = request["messages"]
            if number == 0:
                functions = [tool["function"] for tool in request["tools"]]
                names = [tool["name"] for tool in functions]
                assert len(names) == len(set(names)), names
                assert all(0 < len(name) <= 64 for name in names), names
                assert all(all(c.isalnum() or c in "_-" for c in name) for name in names), names
                assert len([f for f in functions if "paginated state" in f.get("description", "")]) == 2
                calls = []
                for kind, target in (("ha", "desk"), ("windows", "desktop")):
                    function = next(f for f in functions if f.get("description", "").endswith(f"Independent smoke {kind} action"))
                    assert function["name"].startswith(kind + "__"), function
                    calls.append({"id": f"call_{kind}", "type": "function", "function": {
                        "name": function["name"], "arguments": json.dumps({"target": target}),
                    }})
                if state.timeout_windows:
                    calls.append({"id": "call_ha_after_windows", "type": "function", "function": dict(calls[0]["function"])})
                message = {"role": "assistant", "content": None, "tool_calls": calls}
            elif number == 1:
                outputs = [m for m in messages if m["role"] == "tool"]
                assert {m["tool_call_id"] for m in outputs} == {"call_ha", "call_windows"}, outputs
                assert "light_on" in json.dumps(outputs)
                assert "desktop_observed" in json.dumps(outputs)
                image_index = next(i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m.get("content"), list)
                                   and any(p.get("type") == "image_url" for p in m["content"]))
                assert image_index > max(i for i, m in enumerate(messages) if m["role"] == "tool")
                image_parts = messages[image_index]["content"]
                assert any(p.get("type") == "image_url" and p["image_url"]["url"] == "data:image/png;base64," + PIXEL for p in image_parts)
                message = {"role": "assistant", "content": "The desk light is on and the desktop was observed."}
            elif number == 2:
                assert any(m["role"] == "user" and m.get("content") == "Get my workspace ready." for m in messages)
                assert messages[-1] == {"role": "user", "content": "What did you just do?"}
                message = {"role": "assistant", "content": "I turned on the desk light and observed your desktop."}
            else:
                raise AssertionError(f"Unexpected duplicate model request {number}")
            self.reply(200, {"id": f"completion-{number}", "object": "chat.completion", "created": 0,
                             "model": "fake-vision-model", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}" + ("/v1/chat/completions" if kind == "cloud" else "/mcp/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def exercise(urls, state):
    from athena_agent.agent import Agent
    from athena_agent.config import AgentConfig, ServerConfig

    config = AgentConfig(
        model_url=urls["cloud"], model="fake-vision-model", api_key="cloud-test-token",
        servers=tuple(ServerConfig(name=name, url=urls[name], token=f"{name}-test-token") for name in ("ha", "windows")),
        vision=True,
    )
    agent = Agent(config)
    try:
        first = await agent.turn("Get my workspace ready.")
        assert "desk light" in first, (first, state.cloud_errors)
        second = await agent.turn("What did you just do?")
        assert "desktop" in second, second
    finally:
        await agent.close()
    assert not state.auth_failures, state.auth_failures
    assert not state.cloud_errors, state.cloud_errors
    assert len(state.calls) == 2, state.calls
    assert len(state.cloud_requests) == 3, len(state.cloud_requests)
    for kind in ("ha", "windows"):
        assert (kind, "second-page") in state.discoveries, state.discoveries



class AgentHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Threaded fixture shutdown has a 0.5s polling interval per fake service.
        asyncio.get_running_loop().slow_callback_duration = 3

    async def test_cloud_and_two_real_http_mcp_connections(self):
        state = State()
        with ExitStack() as stack:
            urls = {kind: stack.enter_context(serve(kind, state)) for kind in ("cloud", "ha", "windows")}
            await exercise(urls, state)

    async def test_uncertain_execution_stops_batch_without_retry(self):
        from athena_agent.agent import Agent
        from athena_agent.config import AgentConfig, ServerConfig
        state = State(timeout_windows=True)
        with ExitStack() as stack:
            urls = {kind: stack.enter_context(serve(kind, state)) for kind in ("cloud", "ha", "windows")}
            config = AgentConfig(
                model_url=urls["cloud"], model="fake-vision-model", api_key="cloud-test-token",
                servers=tuple(ServerConfig(name=name, url=urls[name], token=f"{name}-test-token") for name in ("ha", "windows")),
                tool_timeout=0.25, vision=True,
            )
            agent = Agent(config)
            try:
                reply = await agent.turn("Get my workspace ready.")
            finally:
                await agent.close()
            self.assertIn("Outcome is unknown", reply)
            self.assertEqual(len(state.calls), 2, state.calls)
            self.assertEqual([kind for kind, _, _ in state.calls], ["ha", "windows"])
            self.assertEqual(len(state.cloud_requests), 1)
            self.assertFalse(state.auth_failures)
            self.assertFalse(state.cloud_errors)


if __name__ == "__main__":
    unittest.main()
