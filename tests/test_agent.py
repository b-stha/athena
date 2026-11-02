"""Agent boundary checks without a microphone, cloud account, or real devices."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from athena_agent.agent import Agent
from athena_agent.config import AgentConfig, ServerConfig
from athena_agent.model import CloudModel, ModelError
from athena_agent.tools import ToolError, ToolRegistry, UncertainToolError, model_tool_name
from athena_agent.worker import AgentWorker


def config(**values):
    return replace(AgentConfig("https://model.example/chat/completions", "test-model", "secret"), **values)


def call(name="ha__switch", args=None, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps({} if args is None else args)}}


class FakeModel:
    def __init__(self, replies):
        self.replies, self.requests, self.closed = list(replies), [], False

    async def complete(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return self.replies.pop(0)

    async def close(self):
        self.closed = True


class FakeRegistry:
    def __init__(self, config):
        self.calls, self.unavailable, self.closed = [], {}, False
        self.error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def definitions(self):
        return [{"type": "function", "function": {"name": "ha__switch", "parameters": {"type": "object"}}}]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return {"content": "Accepted.", "images": []}


class ConfigurationTests(unittest.TestCase):
    def test_secrets_do_not_appear_in_repr(self):
        server = ServerConfig("windows", "http://pc:8000/mcp/", "private-token")
        self.assertNotIn("private-token", repr(server))
        self.assertNotIn("secret", repr(config(servers=(server,))))

    def test_required_configuration_and_limits(self):
        for changes in [{"model": ""}, {"api_key": ""}, {"model_url": "http://cloud.example/v1"},
                        {"max_calls": 0}, {"history_turns": True}, {"tool_timeout": float("nan")},
                        {"vision": "true"}, {"reasoning_effort": "unlimited"},
                        {"reasoning_effort": []}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_local_settings_read_tokens_from_environment_without_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"servers": [{"name": "ha", "url": "http://ha/api/mcp",
                                                     "token_env": "EXAMPLE_HA_TOKEN"}]}))
            with patch.dict(os.environ, {"ATHENA_MODEL_API_URL": "https://model.example/v1/chat/completions",
                                         "ATHENA_MODEL": "test-model", "ATHENA_MODEL_API_KEY": "model-key",
                                         "EXAMPLE_HA_TOKEN": "ha-key"}, clear=True):
                result = AgentConfig.from_env(path)
                self.assertEqual(result.servers[0].token, "ha-key")
                self.assertEqual(result.model, "test-model")

    def test_reasoning_effort_is_explicit_and_loaded_from_json(self):
        self.assertIsNone(config().reasoning_effort)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"servers": [], "reasoning_effort": "low"}))
            with patch.dict(os.environ, {"ATHENA_MODEL_API_URL": "https://model.example/v1/chat/completions",
                                         "ATHENA_MODEL": "test-model", "ATHENA_MODEL_API_KEY": "model-key"}, clear=True):
                self.assertEqual(AgentConfig.from_env(path).reasoning_effort, "low")

    def test_duplicate_server_names_rejected(self):
        server = ServerConfig("ha", "http://ha/api/mcp", "key")
        with self.assertRaises(ValueError):
            config(servers=(server, server))

    def test_non_text_server_names_fail_with_clean_configuration_error(self):
        for name in (None, 42, True, [], {}):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "server names"):
                ServerConfig(name, "http://ha/api/mcp", "key")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, replies, **settings):
        registry = FakeRegistry(None)
        model = FakeModel(replies)
        agent = Agent(config(**settings), model=model, registry_factory=lambda _: registry)
        self.addAsyncCleanup(agent.close)
        return agent, model, registry

    async def test_tool_round_trip_preserves_call_id_and_context(self):
        agent, model, registry = self.make_agent([
            {"role": "assistant", "content": None, "tool_calls": [call(args={"state": "on"})]},
            {"role": "assistant", "content": "Lights are on."},
            {"role": "assistant", "content": "The same lights."}])
        self.assertEqual(await agent.turn("Turn on my lights"), "Lights are on.")
        self.assertEqual(registry.calls, [("ha__switch", {"state": "on"})])
        result = model.requests[1][0][-1]
        self.assertEqual(result, {"role": "tool", "tool_call_id": "call-1", "content": "Accepted."})
        await agent.turn("Which ones?")
        self.assertIn({"role": "user", "content": "Turn on my lights"}, model.requests[2][0])
        self.assertTrue(registry.closed)

    async def test_null_tool_calls_is_final_text(self):
        agent, _, registry = self.make_agent([{"role": "assistant", "content": "Hello", "tool_calls": None}])
        self.assertEqual(await agent.turn("hello"), "Hello")
        self.assertEqual(registry.calls, [])

    async def test_uncertain_execution_stops_batch_without_retries(self):
        agent, model, registry = self.make_agent([{"role": "assistant", "content": None, "tool_calls": [
            call(), call(call_id="call-2")]}])
        registry.error = UncertainToolError("Outcome unknown; not retried.")
        reply = await agent.turn("do two actions")
        self.assertIn("unknown", reply)
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(len(model.requests), 1)
        self.assertTrue(agent._complete_tool_history(agent.history["default"][0]))

    async def test_call_and_reasoning_limits_stop_before_extra_actions(self):
        agent, _, registry = self.make_agent([{"role": "assistant", "content": None, "tool_calls": [call(), call(call_id="2")]}], max_calls=1)
        self.assertIn("action limit", await agent.turn("many actions"))
        self.assertEqual(registry.calls, [])
        agent, _, registry = self.make_agent([{"role": "assistant", "content": None, "tool_calls": [call()]}], max_rounds=1)
        self.assertIn("reasoning limit", await agent.turn("one action"))
        self.assertEqual(len(registry.calls), 1)

    async def test_repeated_call_id_is_rejected_without_second_action(self):
        message = {"role": "assistant", "content": None, "tool_calls": [call()]}
        agent, _, registry = self.make_agent([message, message])
        self.assertIn("invalid or repeated", await agent.turn("act"))
        self.assertEqual(len(registry.calls), 1)

    async def test_malformed_arguments_do_not_dispatch(self):
        malformed = call()
        malformed["function"]["arguments"] = '{"bad": NaN}'
        agent, _, registry = self.make_agent([{"role": "assistant", "tool_calls": [malformed]}])
        self.assertIn("invalid", await agent.turn("act"))
        self.assertEqual(registry.calls, [])

    async def test_history_is_bounded_by_whole_turn_and_session(self):
        agent, model, _ = self.make_agent([{"role": "assistant", "content": "ok"}] * 5, history_turns=1)
        await agent.turn("old")
        await agent.turn("new")
        self.assertEqual(len(agent.history["default"]), 1)
        await agent.turn("other session", "other")
        self.assertNotIn({"role": "user", "content": "new"}, model.requests[2][0])

    async def test_native_wake_works_without_windows_mcp(self):
        agent, model, registry = self.make_agent([
            {"role": "assistant", "tool_calls": [call("native__wake_pc")]},
            {"role": "assistant", "content": "Wake packet sent, startup unconfirmed."}])
        registry.unavailable = {"windows": "unavailable"}
        with patch.dict(os.environ, {"PC_MAC_ADDRESS": "001122334455"}), patch("integrations.desktop.wake_pc") as wake:
            await agent.turn("wake my pc")
            wake.assert_called_once_with()
        self.assertEqual(registry.calls, [])
        self.assertIn("native__wake_pc", [t["function"]["name"] for t in model.requests[0][1]])


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_validation_names_and_partial_availability(self):
        server = ServerConfig("ha", "http://ha/api/mcp", "secret")
        registry = ToolRegistry(config(servers=(server,)))
        class Peer:
            calls = 0
            async def start(self):
                return [Tool(name="switch", inputSchema={"type": "object", "required": ["state"],
                    "properties": {"state": {"enum": ["on", "off"]}}, "additionalProperties": False})]
            async def call(self, name, arguments):
                self.calls += 1
                return CallToolResult(content=[TextContent(type="text", text="done")])
            async def close(self):
                pass
        peer = Peer()
        registry.peers = {"ha": peer}
        async with registry:
            self.assertEqual(registry.definitions()[0]["function"]["name"], "ha__switch")
            with self.assertRaises(ToolError):
                await registry.call("ha__switch", {"state": "delete"})
            with self.assertRaises(ToolError):
                await registry.call("windows__switch", {})
            self.assertEqual(peer.calls, 0)
            self.assertEqual((await registry.call("ha__switch", {"state": "on"}))["content"], "done")
            self.assertEqual(peer.calls, 1)

    async def test_external_schema_references_are_not_retrieved(self):
        registry = ToolRegistry(config())
        class Peer:
            async def start(self):
                return [Tool(name="bad", inputSchema={"$ref": "https://external.example/schema"})]
            async def close(self):
                pass
        registry.peers = {"ha": Peer()}
        async with registry:
            self.assertEqual(registry.definitions(), [])
            self.assertEqual(registry.unavailable, {"ha": "invalid tool catalog"})

    async def test_images_are_explicitly_forwarded_or_explained(self):
        result = CallToolResult(content=[ImageContent(type="image", mimeType="image/png", data=base64.b64encode(b"image").decode())])
        without = ToolRegistry(config()).format_result("windows__screen", result)
        self.assertIn("not forwarded", without["content"])
        with_vision = ToolRegistry(config(vision=True)).format_result("windows__screen", result)
        self.assertTrue(with_vision["images"][0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_safe_names_are_unique_and_bounded(self):
        names = [model_tool_name("windows", name) for name in ["a.b", "a_b", "x" * 200, "x" * 199 + "y"]]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertLessEqual(len(name), 64)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+$")


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_reasoning_and_sequential_tool_signatures_round_trip(self):
        # Response shape follows Google's OpenAI-compatibility signature examples.
        # The signatures are opaque values, not instructions or fabricated replacements.
        signed_calls = []
        for number in (1, 2):
            item = call(call_id=f"call-{number}")
            item["extra_content"] = {"google": {"thought_signature": f"opaque-signature-{number}"}}
            signed_calls.append(item)
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [signed_calls[0]]},
            {"role": "assistant", "content": None, "tool_calls": [signed_calls[1]]},
            {"role": "assistant", "content": "Done.",
             "extra_content": {"google": {"thought_signature": "opaque-final-signature"}}},
            {"role": "assistant", "content": "Both steps."},
        ]
        requests = []

        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertFalse(body["parallel_tool_calls"])
            requests.append(body)
            return httpx.Response(200, json={"choices": [{"message": replies[len(requests) - 1]}]})

        model = CloudModel(config(reasoning_effort="low"))
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        registry = FakeRegistry(None)
        agent = Agent(config(reasoning_effort="low"), model=model, registry_factory=lambda _: registry)
        self.addAsyncCleanup(agent.close)
        self.assertEqual(await agent.turn("Do the two steps."), "Done.")
        first_replay = next(m for m in requests[1]["messages"] if m.get("tool_calls"))
        self.assertEqual(first_replay["tool_calls"], [signed_calls[0]])
        all_replays = [m["tool_calls"][0] for m in requests[2]["messages"] if m.get("tool_calls")]
        self.assertEqual(all_replays, signed_calls)
        self.assertEqual(len(registry.calls), 2)
        await agent.turn("What did you do?")
        previous_answer = next(m for m in requests[3]["messages"] if m.get("content") == "Done.")
        self.assertEqual(previous_answer["extra_content"], replies[2]["extra_content"])

    async def test_reasoning_is_not_sent_when_unconfigured(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hello."}}]})

        model = CloudModel(config())
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(model.close)
        self.assertEqual((await model.complete([{"role": "user", "content": "Hi"}], []))["content"], "Hello.")
        self.assertNotIn("reasoning_effort", requests[0])

    async def test_cloud_timeout_is_not_retried_and_secrets_not_reported(self):
        requests = []
        def handler(request):
            requests.append(request)
            raise httpx.ReadTimeout("secret cloud response", request=request)
        model = CloudModel(config())
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(model.close)
        with self.assertRaises(ModelError) as error:
            await model.complete([{"role": "user", "content": "hi"}], [])
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(len(requests), 1)


class WorkerTests(unittest.TestCase):
    def test_construction_is_lazy_and_work_is_serialized(self):
        responses, done = [], threading.Event()
        class FakeAgent:
            active = 0
            async def turn(self, text, session_id="default"):
                self.active += 1
                self.assert_serial = self.active == 1
                await asyncio.sleep(0.01)
                self.active -= 1
                return text
            async def close(self):
                pass
        fake = FakeAgent()
        worker = AgentWorker(factory=lambda: fake)
        self.assertIsNone(worker.thread)
        def result(value):
            responses.append(value)
            if len(responses) == 2:
                done.set()
        try:
            self.assertTrue(worker.submit("one", result, self.fail))
            self.assertTrue(worker.submit("two", result, self.fail))
            self.assertTrue(done.wait(3))
            self.assertEqual(responses, ["one", "two"])
            self.assertTrue(fake.assert_serial)
        finally:
            worker.close()
        self.assertFalse(worker.submit("three", result, self.fail))

    def test_close_during_factory_prevents_turn_start(self):
        entered, release, turn_started = threading.Event(), threading.Event(), threading.Event()
        class FakeAgent:
            async def turn(self, *args, **kwargs):
                turn_started.set()
            async def close(self):
                pass
        def factory():
            entered.set()
            release.wait(3)
            return FakeAgent()
        worker = AgentWorker(factory=factory)
        worker.submit("act", lambda _: None, self.fail)
        self.assertTrue(entered.wait(3))
        closer = threading.Thread(target=worker.close)
        closer.start()
        for _ in range(100):
            if worker.closed:
                break
            time.sleep(0.005)
        release.set()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertFalse(turn_started.is_set())
        self.assertFalse(worker.thread.is_alive())

    def test_close_cancels_inflight_turn(self):
        entered, cancelled, closed = threading.Event(), threading.Event(), threading.Event()
        class FakeAgent:
            async def turn(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()
            async def close(self):
                closed.set()
        worker = AgentWorker(factory=FakeAgent)
        worker.submit("act", lambda _: self.fail("Unexpected response"), self.fail)
        self.assertTrue(entered.wait(3))
        worker.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(closed.is_set())


if __name__ == "__main__":
    unittest.main()
