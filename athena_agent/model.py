"""Cloud Chat Completions-compatible function calling, without automatic retries."""

import json

import httpx


class ModelError(RuntimeError):
    pass


class CloudModel:
    def __init__(self, config):
        self.config = config
        self.client = httpx.AsyncClient(timeout=config.model_timeout,
                                        headers={"Authorization": "Bearer " + config.api_key},
                                        follow_redirects=False)

    async def complete(self, messages, tools):
        body = {"model": self.config.model, "messages": messages, "stream": False,
                "parallel_tool_calls": False}
        if self.config.reasoning_effort is not None:
            body["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            body.update(tools=tools, tool_choice="auto")
        try:
            async with self.client.stream("POST", self.config.model_url, json=body) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1_000_000:
                        raise ModelError("Model response exceeded the size limit.")
            result = json.loads(data)
            message = result["choices"][0]["message"]
        except httpx.TimeoutException:
            raise ModelError("Cloud model timed out; the model request was not retried.") from None
        except httpx.HTTPStatusError as error:
            raise ModelError(f"Cloud model returned HTTP {error.response.status_code}.") from None
        except httpx.HTTPError:
            raise ModelError("Could not reach the cloud model.") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("Cloud model returned an invalid Chat Completions response.") from None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelError("Cloud model response must contain an assistant message.")
        # Keep opaque provider metadata as returned, including Gemini thought
        # signatures nested in tool_calls. It must survive the next tool round.
        return {key: message[key] for key in ("role", "content", "tool_calls", "extra_content") if key in message}

    async def close(self):
        await self.client.aclose()
