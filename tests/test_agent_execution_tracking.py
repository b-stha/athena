"""Execution receipts survive model failures without any real tool effects."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import threading
import unittest
from unittest.mock import patch

from athena_agent.agent import Agent, ToolExecutionRecord, TurnExecutionResult
from athena_agent.config import AgentConfig
from athena_agent.model import ModelError
from athena_agent.tools import ToolError, UncertainToolError


def tool_call(name="ha__switch", call_id="one"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def batch(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def answer(text="Done"):
    return {"role": "assistant", "content": text}


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response()
        return response

    async def close(self):
        pass


class Registry:
    unavailable = {}

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.enter_error = None

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *args):
        pass

    def definitions(self):
        return [{'type': 'function', 'function': {'name': 'ha__switch',
                'parameters': {'type': 'object'}}}]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        result = self.results.pop(0) if self.results else {"content": "Accepted.", "images": []}
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result()
        return result


class ExecutionTrackingTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, responses, *, results=(), **settings):
        model, registry = Model(responses), Registry(results)
        config = replace(AgentConfig("https://model.example/chat/completions", "test", "private"), **settings)
        agent = Agent(config, model=model, registry_factory=lambda _: registry)
        self.addAsyncCleanup(agent.close)
        return agent, model, registry

    async def test_generic_response_is_returned_even_when_model_claims_completion(self):
        agent, _, _ = self.make_agent([batch(tool_call()), answer("Everything completed")])
        self.assertEqual(await agent.turn("  do it  ", session_id="room"), "Everything completed")
        result = agent.last_execution
        self.assertIsInstance(result, TurnExecutionResult)
        self.assertEqual((result.session_id, result.text, result.status), ("room", "do it", "returned"))
        self.assertEqual(result.model_request_attempts, 2)
        self.assertEqual(result.tool_records, (ToolExecutionRecord("one", "ha__switch", "returned", "Accepted."),))
        self.assertIs(agent.execution_result("room"), result)
        with self.assertRaises(FrozenInstanceError):
            result.tool_records[0].status = "accepted"

    async def test_model_error_after_tool_keeps_receipt_and_partial_summary(self):
        agent, model, registry = self.make_agent([
            batch(tool_call()), ModelError("Cloud model returned HTTP 429.")])
        reply = await agent.turn("turn on lights")
        self.assertIn("429", reply)
        self.assertIn("ha__switch", reply)
        self.assertIn("returned a response; completion is unverified", reply)
        self.assertEqual(agent.last_execution.reply, reply)
        self.assertEqual(agent.last_execution.status, "failed")
        self.assertEqual(agent.last_execution.tool_records[0].status, "returned")
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(agent._complete_tool_history(agent.history["default"][0]))

    async def test_model_error_before_tool_records_no_tool_effects(self):
        agent, _, registry = self.make_agent([ModelError("Cloud model returned HTTP 429.")])
        self.assertEqual(await agent.turn("act"), "Cloud model returned HTTP 429.")
        self.assertEqual(agent.last_execution.tool_records, ())
        self.assertEqual(agent.last_execution.model_request_attempts, 1)
        self.assertEqual(agent.last_execution.status, "failed")
        self.assertEqual(registry.calls, [])

    async def test_unexpected_model_failure_after_tool_preserves_safe_receipt(self):
        agent, model, registry = self.make_agent([batch(tool_call()), RuntimeError("private failure detail")])
        with self.assertRaises(RuntimeError):
            await agent.turn("act")
        result = agent.last_execution
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.tool_records[0].status, "returned")
        self.assertIn("ha__switch", result.reply)
        self.assertNotIn("private", result.reply)
        self.assertEqual(len(registry.calls), 1)
        model.responses.append(answer("Previous response received; completion unverified"))
        await agent.turn("what happened?")
        self.assertTrue(any("Recorded tool outcomes" in message.get("content", "")
                            for message in model.requests[-1] if isinstance(message.get("content"), str)))
        self.assertFalse(any(message.get("tool_calls") for message in model.requests[-1]))
        self.assertEqual(len(registry.calls), 1)

    async def test_late_invalid_or_repeated_call_keeps_earlier_tool_summary(self):
        invalid = tool_call(call_id="two")
        invalid["function"]["arguments"] = '{"value":NaN}'
        for later in (tool_call(), invalid):
            with self.subTest(later=later):
                agent, _, registry = self.make_agent([batch(tool_call()), batch(later)])
                reply = await agent.turn("act")
                self.assertIn("invalid or repeated", reply)
                self.assertIn("ha__switch", reply)
                self.assertEqual(len(registry.calls), 1)
                self.assertEqual(len(agent.last_execution.tool_records), 1)

    async def test_action_and_reasoning_limits_include_previous_tool_outcomes(self):
        cases = [([batch(tool_call()), batch(tool_call(call_id="two"))], {"max_calls": 1}, "action limit"),
                 ([batch(tool_call())], {"max_rounds": 1}, "reasoning limit")]
        for responses, settings, reason in cases:
            with self.subTest(reason=reason):
                agent, _, registry = self.make_agent(responses, **settings)
                reply = await agent.turn("act")
                self.assertIn(reason, reply)
                self.assertIn("ha__switch", reply)
                self.assertIn("Remaining actions were not executed", reply)
                self.assertEqual(agent.last_execution.status, "limit")
                self.assertEqual(len(registry.calls), 1)

    async def test_unknown_outcome_stops_batch_and_keeps_skipped_receipts(self):
        agent, model, registry = self.make_agent([batch(tool_call(), tool_call(call_id="two"))],
            results=[UncertainToolError("Outcome unknown; not retried.")])
        reply = await agent.turn("act twice")
        self.assertIn("unknown", reply)
        self.assertEqual(agent.last_execution.status, "unknown")
        self.assertEqual([record.status for record in agent.last_execution.tool_records], ["unknown", "not_started"])
        self.assertEqual((len(registry.calls), len(model.requests)), (1, 1))
        self.assertTrue(agent._complete_tool_history(agent.history["default"][0]))

    async def test_reported_mcp_error_is_unknown_and_stops_following_action(self):
        agent, model, registry = self.make_agent([batch(tool_call(), tool_call(call_id="two"))],
            results=[{"content": "Server reported an error", "images": [], "execution_status": "unknown"}])
        reply = await agent.turn("act twice")
        self.assertIn("unknown", reply)
        self.assertEqual([record.status for record in agent.last_execution.tool_records], ["unknown", "not_started"])
        self.assertEqual((len(registry.calls), len(model.requests)), (1, 1))

    async def test_known_preflight_rejection_is_not_an_accepted_or_unknown_effect(self):
        agent, _, registry = self.make_agent([batch(tool_call()), answer("Rejected")],
            results=[ToolError("Schema mismatch; no action sent.")])
        self.assertEqual(await agent.turn("act"), "Rejected")
        self.assertEqual(agent.last_execution.tool_records[0].status, "rejected")
        self.assertEqual(len(registry.calls), 1)

    async def test_malformed_result_is_unknown_without_retry_or_later_batch_effect(self):
        agent, model, registry = self.make_agent([batch(tool_call(), tool_call(call_id="two"))],
            results=[{"content": "response", "images": None}])
        self.assertIn("unknown", await agent.turn("act twice"))
        self.assertEqual([record.status for record in agent.last_execution.tool_records], ["unknown", "not_started"])
        self.assertEqual((len(registry.calls), len(model.requests)), (1, 1))

    async def test_native_wake_acceptance_only_records_packet_dispatch(self):
        agent, _, registry = self.make_agent([batch(tool_call("native__wake_pc")), answer("Wake requested")])
        with patch.dict("os.environ", {"PC_MAC_ADDRESS": "001122334455"}), patch("integrations.desktop.wake_pc") as wake:
            await agent.turn("wake my pc")
        wake.assert_called_once_with()
        self.assertEqual(registry.calls, [])
        self.assertEqual(agent.last_execution.tool_records[0].status, "accepted")
        self.assertIn("startup is not confirmed", agent.last_execution.tool_records[0].result_excerpt)

    async def test_native_failure_is_unknown_and_stops_remaining_batch(self):
        agent, model, registry = self.make_agent([batch(tool_call("native__wake_pc"), tool_call(call_id="two"))])
        with patch.dict("os.environ", {"PC_MAC_ADDRESS": "001122334455"}), \
                patch("integrations.desktop.wake_pc", side_effect=OSError("private detail")):
            reply = await agent.turn("wake pc then switch")
        self.assertIn("unknown", reply)
        self.assertNotIn("private", reply)
        self.assertEqual([record.status for record in agent.last_execution.tool_records], ["unknown", "not_started"])
        self.assertEqual(registry.calls, [])
        self.assertEqual(len(model.requests), 1)

    async def test_cancellation_during_tool_publishes_unknown_then_propagates(self):
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        agent, _, registry = self.make_agent([batch(tool_call(), tool_call(call_id="two"))], results=[blocked])
        task = asyncio.create_task(agent.turn("act twice", session_id="room"))
        await entered.wait()
        running = agent.last_execution
        self.assertEqual(running.tool_records[0].status, "dispatched")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = agent.last_execution
        self.assertIsNot(result, running)
        self.assertEqual((result.status, result.session_id), ("cancelled", "room"))
        self.assertEqual(result.tool_records[0].status, "unknown")
        self.assertIn("unknown", result.reply)
        self.assertEqual(len(registry.calls), 1)

    async def test_cancellation_during_followup_model_preserves_returned_tool(self):
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        agent, _, registry = self.make_agent([batch(tool_call()), blocked])
        task = asyncio.create_task(agent.turn("act"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(agent.last_execution.status, "cancelled")
        self.assertEqual(agent.last_execution.tool_records[0].status, "returned")
        self.assertIn("ha__switch", agent.last_execution.reply)
        self.assertEqual(len(registry.calls), 1)

    async def test_native_cancellation_remains_unknown_while_worker_can_finish(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def wake():
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)
            loop.call_soon_threadsafe(finished.set)
        agent, _, registry = self.make_agent([batch(tool_call("native__wake_pc"), tool_call(call_id="two"))])
        try:
            with patch.dict("os.environ", {"PC_MAC_ADDRESS": "001122334455"}), \
                    patch("integrations.desktop.wake_pc", side_effect=wake):
                task = asyncio.create_task(agent.turn("wake pc then switch"))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(agent.last_execution.status, "cancelled")
                self.assertEqual(agent.last_execution.tool_records[0].status, "unknown")
                self.assertEqual(registry.calls, [])
                release.set()
                await finished.wait()
                self.assertEqual(agent.last_execution.tool_records[0].status, "unknown")
        finally:
            release.set()

    async def test_later_question_sees_interrupted_outcomes_without_incomplete_tool_batch(self):
        for during_tool in (True, False):
            with self.subTest(during_tool=during_tool):
                entered = asyncio.Event()
                async def blocked():
                    entered.set()
                    await asyncio.Event().wait()
                responses = [batch(tool_call())] if during_tool else [batch(tool_call()), blocked]
                results = [blocked] if during_tool else []
                agent, model, registry = self.make_agent(responses, results=results)
                task = asyncio.create_task(agent.turn("act", session_id="room"))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                previous = agent.history["room"][0]
                self.assertEqual(previous[0], {"role": "user", "content": "act"})
                self.assertEqual(len(previous), 2)
                self.assertIn("unknown" if during_tool else "returned a response", previous[1]["content"])
                self.assertIn("Do not repeat", previous[1]["content"])
                self.assertTrue(agent._complete_tool_history(previous))
                model.responses.append(answer("Previous outcome summarized"))
                await agent.turn("what happened?", session_id="room")
                self.assertIn(previous[1], model.requests[-1])
                self.assertFalse(any(message.get("tool_calls") for message in model.requests[-1]))
                self.assertEqual(len(registry.calls), 1)

    async def test_unexpected_tool_failure_remains_unknown_and_readable(self):
        agent, _, registry = self.make_agent([batch(tool_call(), tool_call(call_id="two"))],
            results=[RuntimeError("private")])
        with self.assertRaises(RuntimeError):
            await agent.turn("act twice")
        self.assertEqual(agent.last_execution.status, "failed")
        self.assertEqual(agent.last_execution.tool_records[0].status, "unknown")
        self.assertIn("unknown", agent.last_execution.reply)
        self.assertNotIn("private", agent.last_execution.reply)
        self.assertEqual(len(registry.calls), 1)

    async def test_new_failed_turn_has_fresh_session_snapshot_without_stale_tools(self):
        agent, _, registry = self.make_agent([batch(tool_call()), answer()])
        await agent.turn("first", session_id="room")
        previous = agent.last_execution
        registry.enter_error = RuntimeError("private discovery detail")
        with self.assertRaises(RuntimeError):
            await agent.turn(" second ", session_id="other")
        result = agent.last_execution
        self.assertIsNot(result, previous)
        self.assertEqual((result.text, result.session_id, result.tool_records), ("second", "other", ()))
        self.assertIs(agent.execution_result("room"), previous)
        self.assertIs(agent.execution_result("other"), result)

    async def test_session_receipts_are_bounded_and_excerpts_do_not_store_raw_arguments(self):
        responses = []
        for _ in range(9):
            responses.extend([batch(tool_call()), answer()])
        agent, _, _ = self.make_agent(responses, results=[{"content": "x" * 1000, "images": []}] * 9)
        for index in range(9):
            await agent.turn("act", session_id=str(index))
        self.assertIsNone(agent.execution_result("0"))
        self.assertEqual(len(agent._execution_results), 8)
        self.assertEqual(len(agent.last_execution.tool_records[0].result_excerpt), 512)
        self.assertEqual(agent.last_execution.model_request_attempts, 2)


if __name__ == "__main__":
    unittest.main()
