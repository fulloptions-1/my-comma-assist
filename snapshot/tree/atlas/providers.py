"""Model provider boundary.

The kernel sees only this contract: typed requests in, typed results out,
errors normalized to categories. Everything provider-specific — wire formats,
auth, quirks — stays inside an adapter. Any future adapter (including a
legacy browser-transport one) must fit behind this same interface.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# ------------------------------------------------------------------ contract


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict[str, Any]  # JSON-schema "properties"-style: {field: {type, required}}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderMessage:
    role: str  # "user" | "assistant" | "tool"
    content: str = ""
    tool_call_id: str | None = None  # for role="tool" results
    tool_calls: tuple[ToolCall, ...] = ()  # for assistant turns that called tools

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ]
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ProviderMessage":
        return ProviderMessage(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            tool_call_id=data.get("tool_call_id"),
            tool_calls=tuple(
                ToolCall(
                    id=str(c.get("id", "")),
                    name=str(c.get("name", "")),
                    arguments=dict(c.get("arguments", {})),
                )
                for c in data.get("tool_calls", [])
            ),
        )


@dataclass(frozen=True)
class ProviderRequest:
    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolDef, ...] = ()
    model: str = ""
    system: str = ""
    max_tokens: int = 1024
    timeout_s: float | None = None  # per-call override of the adapter default


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ProviderResult:
    finish_reason: str  # "end" | "tool_use" | "max_tokens"
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)


PROVIDER_MAX_RESPONSE_BYTES = 5_000_000  # cap before parsing (M1.4 §9)

_TOOL_WIRE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _wire_tool_name(name: str) -> str:
    """Return a provider-safe tool name while preserving a reversible mapping.

    Atlas uses namespaced internal IDs such as ``user.ask`` and
    ``car.read_history``. Anthropic and OpenAI-compatible function/tool APIs
    accept only ASCII letters, digits, ``_`` and ``-``. A short hash prevents
    collisions after punctuation is normalized.
    """
    if _TOOL_WIRE_NAME_RE.fullmatch(name):
        return name
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "tool"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    base = base[: 64 - len(digest) - 1]
    return f"{base}_{digest}"


def _tool_name_maps(tools: tuple[ToolDef, ...]) -> tuple[dict[str, str], dict[str, str]]:
    internal_to_wire: dict[str, str] = {}
    wire_to_internal: dict[str, str] = {}
    for tool in tools:
        wire = _wire_tool_name(tool.name)
        previous = wire_to_internal.get(wire)
        if previous is not None and previous != tool.name:
            raise ProviderError(
                "malformed",
                f"tool-name collision between {previous!r} and {tool.name!r}",
                retryable=False,
            )
        internal_to_wire[tool.name] = wire
        wire_to_internal[wire] = tool.name
    return internal_to_wire, wire_to_internal


def _map_outbound_tool_name(name: str, mapping: dict[str, str]) -> str:
    # Historical assistant tool calls should refer to a declared tool. Falling
    # back to deterministic encoding keeps replay robust if a caller constructs
    # a request manually.
    return mapping.get(name, _wire_tool_name(name))


def _reject_json_constant(name: str):
    """json.loads hook: NaN/Infinity/-Infinity are not standard JSON and
    never legitimate in provider responses or tool arguments (M1.4 §8)."""
    raise ValueError(f"nonstandard JSON constant: {name}")


def _usage_int(usage: dict, key: str) -> int:
    """Usage fields must be finite non-negative integers (M1.4 §5): bools,
    strings, floats, and negatives are provider-protocol violations, not
    excuses for a raw ValueError to escape the adapter boundary."""
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(
            "malformed", f"usage.{key} must be an integer, got {type(value).__name__}"
        )
    if value < 0:
        raise ProviderError("malformed", f"usage.{key} must be >= 0, got {value}")
    return value


def _guard_response_size(text: str) -> None:
    if len(text) > PROVIDER_MAX_RESPONSE_BYTES:
        raise ProviderError(
            "malformed",
            f"response exceeds {PROVIDER_MAX_RESPONSE_BYTES} bytes; refusing to parse",
        )


ERROR_CATEGORIES = frozenset({"timeout", "rate_limit", "auth", "malformed", "transport"})


class ProviderError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool | None = None):
        if category not in ERROR_CATEGORIES:
            category = "transport"
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.retryable = (
            retryable
            if retryable is not None
            else category in {"timeout", "rate_limit", "transport"}
        )


class Provider(Protocol):
    id: str

    def complete(self, request: ProviderRequest) -> ProviderResult: ...


# ------------------------------------------------------------- fake provider


class FakeProvider:
    """Deterministic provider for offline CI and keyless use.

    With a script, returns/raises each entry in order (asserting on requests
    is up to the test). Without a script — the packaged keyless default — it
    deterministically echoes the last user/tool message as final text.
    """

    id = "fake"

    def __init__(self, script: list[ProviderResult | ProviderError] | None = None):
        self.script = list(script) if script else None
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        if self.script is not None:
            if not self.script:
                raise ProviderError("malformed", "FakeProvider script exhausted")
            step = self.script.pop(0)
            if isinstance(step, ProviderError):
                raise step
            return step
        last = ""
        for message in reversed(request.messages):
            if message.role in ("user", "tool"):
                last = message.content
                break
        return ProviderResult(
            finish_reason="end",
            text=f"FAKE({request.model or 'fake'}): {last}"[:400],
            usage=Usage(input_tokens=len(request.messages), output_tokens=1),
        )


# -------------------------------------------------------- anthropic provider


HttpPost = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


def _object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    properties = {
        name: {"type": spec.get("type", "string")} for name, spec in schema.items()
    }
    required = [name for name, spec in schema.items() if spec.get("required")]
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _raise_for_status(status: int, text: str) -> None:
    if status in (401, 403):
        raise ProviderError("auth", f"HTTP {status}: {text[:200]}")
    if status == 429:
        raise ProviderError("rate_limit", f"HTTP {status}: {text[:200]}")
    if 400 <= status < 500:
        raise ProviderError("malformed", f"HTTP {status}: {text[:200]}", retryable=False)
    if status >= 500:
        raise ProviderError("transport", f"HTTP {status}: {text[:200]}")


def _read_capped(stream) -> bytes:
    """Read at most PROVIDER_MAX_RESPONSE_BYTES from a network stream,
    measured in BYTES, stopping before an oversized body is ever fully
    requested or stored (M1.5 §4). Loops because socket reads may return
    short before EOF."""
    limit = PROVIDER_MAX_RESPONSE_BYTES
    chunks: list[bytes] = []
    remaining = limit + 1  # one extra byte proves the overflow
    while remaining > 0:
        chunk = stream.read(min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ProviderError(
            "malformed",
            f"response exceeds {limit} bytes; refusing to read further",
        )
    return data


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _read_capped(response)          # bytes checked BEFORE decode
            return response.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # non-2xx still carries a body
        raw = _read_capped(exc)
        return exc.code, raw.decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise ProviderError("timeout", str(exc)) from exc
    except urllib.error.URLError as exc:
        raise ProviderError("transport", str(exc)) from exc


class AnthropicProvider:
    """Adapter for the Anthropic Messages API with typed tool use.

    The HTTP transport is injectable so request/response mapping is fully
    unit-testable from recorded fixtures without a key or network access.
    """

    id = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        http_post: HttpPost | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.http_post = http_post or _urllib_post
        self.timeout = timeout

    # -- request mapping ----------------------------------------------------

    def _to_wire(self, request: ProviderRequest) -> dict[str, Any]:
        internal_to_wire, _ = _tool_name_maps(request.tools)
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": _map_outbound_tool_name(call.name, internal_to_wire),
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                messages.append({"role": "assistant", "content": blocks})
            elif message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
            else:
                messages.append({"role": message.role, "content": message.content})

        body: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system:
            body["system"] = request.system
        if request.tools:
            body["tools"] = [
                {
                    "name": internal_to_wire[tool.name],
                    "description": tool.description,
                    "input_schema": self._input_schema(tool.schema),
                }
                for tool in request.tools
            ]
        return body

    @staticmethod
    def _input_schema(schema: dict[str, Any]) -> dict[str, Any]:
        return _object_schema(schema)

    # -- response mapping ---------------------------------------------------

    def complete(self, request: ProviderRequest) -> ProviderResult:
        _, wire_to_internal = _tool_name_maps(request.tools)
        body = json.dumps(self._to_wire(request)).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.VERSION,
        }
        status, text = self.http_post(
            self.URL, headers, body, request.timeout_s or self.timeout
        )
        _raise_for_status(status, text)
        _guard_response_size(text)
        try:
            data = json.loads(text, parse_constant=_reject_json_constant)
            blocks = data.get("content", [])
            text_parts: list[str] = []
            for block in blocks:
                if block.get("type") != "text":
                    continue
                piece = block.get("text", "")
                if not isinstance(piece, str):
                    raise ProviderError(
                        "malformed",
                        f"text block content must be a string, got {type(piece).__name__}",
                    )
                text_parts.append(piece)
            text_out = "".join(text_parts)
            for block in blocks:
                if block.get("type") == "tool_use" and not isinstance(
                    block.get("input", {}), dict
                ):
                    raise ProviderError(
                        "malformed",
                        "tool_use input must be a JSON object, got "
                        f"{type(block.get('input')).__name__}",
                    )
            calls = tuple(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=wire_to_internal.get(
                        str(block.get("name", "")), str(block.get("name", ""))
                    ),
                    arguments=dict(block.get("input", {})),
                )
                for block in blocks
                if block.get("type") == "tool_use"
            )
            stop = str(data.get("stop_reason", "end_turn"))
            usage = data.get("usage", {})
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderError("malformed", f"unparsable response: {exc}") from exc
        known = {
            "end_turn": "end",
            "stop_sequence": "end",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }
        if stop not in known:
            # An unknown wire reason means the protocol moved under us; a
            # silent "end" could truncate work invisibly (M1.2 §8).
            raise ProviderError("malformed", f"unknown stop_reason: {stop!r}")
        finish = known[stop]
        return ProviderResult(
            finish_reason=finish,
            text=text_out,
            tool_calls=calls,
            usage=Usage(
                input_tokens=_usage_int(usage, "input_tokens"),
                output_tokens=_usage_int(usage, "output_tokens"),
            ),
        )


# ------------------------------------------------- openai-compatible provider


class OpenAICompatProvider:
    """Adapter for OpenAI-compatible chat-completions endpoints.

    Works against any server implementing the /chat/completions wire format
    (OpenAI, many local/gateway servers) with a configurable base URL. The
    HTTP transport is injectable, so mapping is unit-tested from fixtures;
    live verification requires a real key (account-blocked for the reviewer).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "",
        http_post: HttpPost | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.id = "openai_compat"
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http_post = http_post or _urllib_post
        self.timeout = timeout

    def _to_wire(self, request: ProviderRequest) -> dict[str, Any]:
        internal_to_wire, _ = _tool_name_maps(request.tools)
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            if message.role == "assistant" and message.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": _map_outbound_tool_name(
                                        call.name, internal_to_wire
                                    ),
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            elif message.role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content,
                    }
                )
            else:
                messages.append({"role": message.role, "content": message.content})

        body: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": internal_to_wire[tool.name],
                        "description": tool.description,
                        "parameters": _object_schema(tool.schema),
                    },
                }
                for tool in request.tools
            ]
        return body

    def complete(self, request: ProviderRequest) -> ProviderResult:
        _, wire_to_internal = _tool_name_maps(request.tools)
        body = json.dumps(self._to_wire(request)).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}/chat/completions"
        status, text = self.http_post(
            url, headers, body, request.timeout_s or self.timeout
        )
        _raise_for_status(status, text)
        _guard_response_size(text)
        try:
            data = json.loads(text, parse_constant=_reject_json_constant)
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ProviderError(
                    "malformed",
                    f"message content must be a string, got {type(content).__name__}",
                )
            text_out = content or ""
            calls: list[ToolCall] = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function", {})
                raw_args = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args, parse_constant=_reject_json_constant)
                except ValueError as exc:
                    raise ProviderError(
                        "malformed", f"tool arguments are not JSON: {raw_args[:120]}"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ProviderError(
                        "malformed",
                        f"tool arguments must be a JSON object, got {type(arguments).__name__}",
                    )
                calls.append(
                    ToolCall(
                        id=str(raw_call.get("id", "")),
                        name=wire_to_internal.get(
                            str(function.get("name", "")),
                            str(function.get("name", "")),
                        ),
                        arguments=dict(arguments),
                    )
                )
            stop = str(choice.get("finish_reason", "stop"))
            usage = data.get("usage", {}) or {}
        except ProviderError:
            raise
        except (ValueError, LookupError, AttributeError, TypeError) as exc:
            raise ProviderError("malformed", f"unparsable response: {exc}") from exc
        known = {
            "stop": "end",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        if stop not in known:
            raise ProviderError("malformed", f"unknown finish_reason: {stop!r}")
        finish = known[stop]
        return ProviderResult(
            finish_reason=finish,
            text=text_out,
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=_usage_int(usage, "prompt_tokens"),
                output_tokens=_usage_int(usage, "completion_tokens"),
            ),
        )


def probe(provider: Provider, model: str = "") -> dict[str, Any]:
    """One tiny authenticated call so the user can test a pasted key.

    Returns only redacted status: never key material, and error detail is
    truncated. Callers surface this dict verbatim to the UI (M1.1 §9).
    """
    request = ProviderRequest(
        messages=(ProviderMessage("user", "Reply with the single word: pong"),),
        model=model,
        max_tokens=16,
        timeout_s=20.0,
    )
    started = time.monotonic()
    try:
        result = provider.complete(request)
    except ProviderError as exc:
        return {
            "ok": False,
            "provider": provider.id,
            "category": exc.category,
            "detail": str(exc)[:200],
        }
    return {
        "ok": True,
        "provider": provider.id,
        "finish_reason": result.finish_reason,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "snippet": (result.text or "")[:80],
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
    }
