"""Explicit model and MCP configuration. Loading performs no network calls."""

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def _url(value, label, *, cloud=False):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a URL.")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment or parsed.query):
        raise ValueError(f"{label} must be an HTTP(S) URL without credentials, query or fragment.")
    if cloud and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("The cloud model endpoint requires HTTPS.")
    return value


@dataclass(frozen=True)
class ServerConfig:
    name: str
    url: str
    token: str = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", self.name)
                or self.name == "native"):
            raise ValueError("MCP server names must be short lowercase identifiers; native is reserved.")
        _url(self.url, "MCP endpoint")
        if not isinstance(self.token, str) or not self.token.strip() or any(c in self.token for c in "\r\n"):
            raise ValueError(f"Set the bearer token for MCP server {self.name}.")


@dataclass(frozen=True)
class AgentConfig:
    model_url: str
    model: str
    api_key: str = field(repr=False)
    servers: tuple[ServerConfig, ...] = ()
    model_timeout: float = 60
    tool_timeout: float = 30
    discovery_timeout: float = 5
    max_rounds: int = 8
    max_calls: int = 16
    history_turns: int = 6
    history_chars: int = 48000
    max_result_chars: int = 16000
    max_image_bytes: int = 4_000_000
    max_images: int = 4
    vision: bool = False
    system_prompt: str = ""
    reasoning_effort: str | None = None

    def __post_init__(self):
        _url(self.model_url, "Model endpoint", cloud=True)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Set ATHENA_MODEL to a cloud model supporting function calling.")
        if not isinstance(self.api_key, str) or not self.api_key.strip() or any(c in self.api_key for c in "\r\n"):
            raise ValueError("Set ATHENA_MODEL_API_KEY.")
        if len({s.name for s in self.servers}) != len(self.servers):
            raise ValueError("MCP server names must be unique.")
        for name in ("model_timeout", "tool_timeout", "discovery_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number.")
        for name, maximum in (("max_rounds", 32), ("max_calls", 64), ("history_turns", 50),
                              ("history_chars", 200000), ("max_result_chars", 64000),
                              ("max_image_bytes", 20_000_000), ("max_images", 8)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
        if not isinstance(self.vision, bool) or not isinstance(self.system_prompt, str):
            raise ValueError("vision must be boolean and system_prompt must be text.")
        if (self.reasoning_effort is not None
                and (not isinstance(self.reasoning_effort, str)
                     or self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"})):
            raise ValueError("reasoning_effort must be a supported effort name or null.")

    @classmethod
    def from_env(cls, config_path=None):
        path = Path(config_path or os.getenv("ATHENA_AGENT_CONFIG", "agent.local.json"))
        if not path.is_file():
            raise ValueError("Create agent.local.json from agent.example.json, or set ATHENA_AGENT_CONFIG.")
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("Could not read the agent JSON configuration.") from None
        if not isinstance(settings, dict):
            raise ValueError("Agent configuration must be a JSON object.")
        known = {"servers", "model_timeout", "tool_timeout", "discovery_timeout", "max_rounds", "max_calls", "history_turns",
                 "history_chars", "max_result_chars", "max_image_bytes", "max_images", "vision", "system_prompt",
                 "reasoning_effort"}
        if settings.keys() - known:
            raise ValueError("Unknown agent configuration fields: " + ", ".join(sorted(settings.keys() - known)))
        raw_servers = settings.pop("servers", [])
        if not isinstance(raw_servers, list):
            raise ValueError("servers must be a list.")
        servers = []
        for item in raw_servers:
            if not isinstance(item, dict) or set(item) != {"name", "url", "token_env"}:
                raise ValueError("Each MCP server requires name, url and token_env.")
            if not isinstance(item["token_env"], str):
                raise ValueError("token_env must name an environment variable.")
            servers.append(ServerConfig(item["name"], item["url"], os.getenv(item["token_env"], "")))
        return cls(os.getenv("ATHENA_MODEL_API_URL", ""), os.getenv("ATHENA_MODEL", ""),
                   os.getenv("ATHENA_MODEL_API_KEY", ""), tuple(servers), **settings)
