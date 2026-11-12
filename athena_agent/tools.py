"""Owned MCP sessions, tool discovery and argument/result boundaries."""

import asyncio
import base64
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import re

import httpx
from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ToolError(RuntimeError):
    pass


class UncertainToolError(ToolError):
    """Execution may have happened; do not retry or continue this action batch."""


def model_tool_name(server, original):
    raw = server + "__" + original
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        return raw
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return safe[:45] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:18]


def _local_refs_only(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise ToolError("Tool schemas may reference only their own document.")
            _local_refs_only(item)
    elif isinstance(value, list):
        for item in value:
            _local_refs_only(item)


@dataclass
class ToolSpec:
    name: str
    original: str
    server: str
    description: str
    schema: dict
    validator: object

    def definition(self):
        return {"type": "function", "function": {"name": self.name,
                "description": f"[{self.server}] {self.description}", "parameters": self.schema}}


class MCPPeer:
    """Enter and exit SDK AnyIO scopes in the same actor task."""

    def __init__(self, server, timeout, discovery_timeout=5):
        self.server = server
        self.timeout = timeout
        self.discovery_timeout = discovery_timeout
        self.queue = asyncio.Queue()
        self.task = None
        self.ready = None

    async def start(self):
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run(), name="mcp-" + self.server.name)
        return await self.ready

    async def _run(self):
        active = None
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + self.server.token},
                timeout=httpx.Timeout(self.timeout, connect=self.discovery_timeout), follow_redirects=False,
            ) as client:
                async with streamable_http_client(self.server.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                        deadline = asyncio.get_running_loop().time() + self.discovery_timeout
                        def remaining():
                            return max(0.001, deadline - asyncio.get_running_loop().time())
                        await asyncio.wait_for(session.initialize(), remaining())
                        tools, cursor, seen = [], None, set()
                        for _ in range(32):
                            page = await asyncio.wait_for(session.list_tools(cursor=cursor), remaining())
                            tools.extend(page.tools)
                            if len(tools) > 256:
                                raise ToolError("MCP server exposed too many tools.")
                            cursor = page.nextCursor
                            if cursor is None:
                                break
                            if cursor in seen:
                                raise ToolError("MCP tool pagination repeated a cursor.")
                            seen.add(cursor)
                        else:
                            raise ToolError("MCP tool pagination exceeded its limit.")
                        self.ready.set_result(tools)
                        while True:
                            item = await self.queue.get()
                            if item is None:
                                return
                            name, arguments, active = item
                            try:
                                result = await asyncio.wait_for(session.call_tool(name, arguments), self.timeout)
                                if not active.done():
                                    active.set_result(result)
                            except Exception:
                                if not active.done():
                                    active.set_exception(UncertainToolError(
                                        f"{self.server.name} connection failed during execution. "
                                        "Outcome is unknown; the action was not retried."))
                                return
                            finally:
                                active = None
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(ToolError(f"MCP server {self.server.name} is unavailable."))
        finally:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(ToolError(f"MCP server {self.server.name} stopped."))
            if active is not None and not active.done():
                active.set_exception(UncertainToolError("Execution was interrupted; outcome is unknown."))
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_exception(ToolError("MCP connection closed before dispatch."))

    async def call(self, name, arguments):
        if self.task is None or self.task.done():
            raise ToolError(f"MCP server {self.server.name} is disconnected; no action sent.")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((name, arguments, future))
        return await future

    async def close(self):
        if self.task is not None:
            if not self.task.done():
                self.queue.put_nowait(None)
            try:
                await asyncio.wait_for(self.task, min(self.timeout + 2, 10))
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


class ToolRegistry:
    def __init__(self, config):
        self.config = config
        self.peers = {server.name: MCPPeer(server, config.tool_timeout, config.discovery_timeout) for server in config.servers}
        self.tools = {}
        self.unavailable = {}

    async def __aenter__(self):
        discovered = await asyncio.gather(*(peer.start() for peer in self.peers.values()), return_exceptions=True)
        for (name, peer), result in zip(self.peers.items(), discovered):
            if isinstance(result, Exception):
                self.unavailable[name] = "unavailable"
                continue
            try:
                specs = []
                originals = set()
                for tool in result:
                    if tool.name in originals:
                        raise ToolError("Duplicate original tool name.")
                    originals.add(tool.name)
                    _local_refs_only(tool.inputSchema)
                    validator_class = validator_for(tool.inputSchema)
                    validator_class.check_schema(tool.inputSchema)
                    mapped = model_tool_name(name, tool.name)
                    if mapped in self.tools or any(s.name == mapped for s in specs):
                        raise ToolError("Tool name collision.")
                    specs.append(ToolSpec(mapped, tool.name, name, (tool.description or tool.name)[:4000],
                                          tool.inputSchema, validator_class(tool.inputSchema)))
                self.tools.update({s.name: s for s in specs})
            except Exception:
                self.unavailable[name] = "invalid tool catalog"
        return self

    async def __aexit__(self, *args):
        await asyncio.gather(*(peer.close() for peer in self.peers.values()), return_exceptions=True)

    def definitions(self):
        return [self.tools[name].definition() for name in sorted(self.tools)]

    async def call(self, name, arguments):
        spec = self.tools.get(name)
        if spec is None:
            raise ToolError("Unknown tool; no action sent.")
        if not isinstance(arguments, dict):
            raise ToolError("Tool arguments must be a JSON object; no action sent.")
        if not spec.validator.is_valid(arguments):
            raise ToolError("Tool arguments failed the discovered JSON schema; no action sent.")
        result = await self.peers[spec.server].call(spec.original, arguments)
        return self.format_result(name, result)

    def format_result(self, name, result):
        text, images = [], []
        if result.isError:
            text.append("The tool reported an execution error; do not assume completion.")
        if result.structuredContent is not None:
            text.append(json.dumps(result.structuredContent, ensure_ascii=False))
        for block in result.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "image":
                if not self.config.vision:
                    text.append("Image returned but not forwarded: configure vision=true and a vision-capable model.")
                    continue
                if block.mimeType not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                    text.append("Image MIME type is unsupported by the model adapter.")
                    continue
                if len(block.data) > (self.config.max_image_bytes + 2) // 3 * 4:
                    text.append("Image exceeds the configured size limit and was not forwarded.")
                    continue
                try:
                    decoded = base64.b64decode(block.data, validate=True)
                except ValueError:
                    text.append("Tool returned invalid image data.")
                    continue
                if len(decoded) > self.config.max_image_bytes or len(images) >= self.config.max_images:
                    text.append("Image limit reached; request a smaller or targeted screenshot.")
                    continue
                images.append({"type": "image_url", "image_url": {
                    "url": f"data:{block.mimeType};base64,{block.data}"}})
            else:
                text.append(f"Unsupported MCP content type: {block.type}; content not forwarded.")
        content = "\n".join(text) or "Tool completed without textual content."
        if len(content) > self.config.max_result_chars:
            content = content[:self.config.max_result_chars] + "\n[Tool result truncated.]"
        return {"content": content, "images": images,
                "execution_status": "unknown" if result.isError else "returned"}
