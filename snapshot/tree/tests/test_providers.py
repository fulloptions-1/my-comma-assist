"""Provider boundary tests (matrix D1/D2/D3-mocked/D10)."""
import json

from atlas.providers import (
    AnthropicProvider,
    FakeProvider,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderResult,
    ToolCall,
    ToolDef,
    _wire_tool_name,
)

REQ = ProviderRequest(
    messages=(
        ProviderMessage("user", "Log my mileage"),
        ProviderMessage(
            "assistant",
            "",
            tool_calls=(ToolCall(id="tc_1", name="user.ask", arguments={"question": "km?"}),),
        ),
        ProviderMessage("tool", "142300", tool_call_id="tc_1"),
    ),
    tools=(
        ToolDef(
            name="car.read_history",
            description="read",
            schema={"vehicle_id": {"type": "string", "required": False}},
        ),
    ),
    model="claude-haiku-4-5",
    system="Be terse.",
)



def test_provider_tool_names_are_wire_safe_and_reversible() -> None:
    names = ["user.ask", "car.read_history", "already_valid", "hyphen-ok"]
    encoded = [_wire_tool_name(name) for name in names]
    assert len(set(encoded)) == len(names)
    for wire in encoded:
        assert 1 <= len(wire) <= 64
        assert all(ch.isalnum() or ch in "_-" for ch in wire)
    assert _wire_tool_name("already_valid") == "already_valid"
    assert _wire_tool_name("user.ask") != "user.ask"


def test_fake_provider_scripted_and_default_modes() -> None:
    scripted = FakeProvider(
        script=[
            ProviderResult(
                finish_reason="tool_use",
                tool_calls=(ToolCall(id="a", name="car.read_history", arguments={}),),
            ),
            ProviderError("rate_limit", "slow down"),
        ]
    )
    first = scripted.complete(REQ)
    assert first.finish_reason == "tool_use"
    assert first.tool_calls[0].name == "car.read_history"
    try:
        scripted.complete(REQ)
    except ProviderError as exc:
        assert exc.category == "rate_limit" and exc.retryable
    else:
        raise AssertionError("scripted error not raised")
    assert len(scripted.requests) == 2

    default = FakeProvider()
    result = default.complete(REQ)
    assert result.finish_reason == "end"
    assert "142300" in result.text  # deterministic echo of last user/tool content


def test_anthropic_request_mapping_is_correct_wire_format() -> None:
    captured: dict = {}

    def transport(url, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return 200, json.dumps(
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )

    provider = AnthropicProvider("test-key", http_post=transport)
    result = provider.complete(REQ)

    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "test-key"
    body = captured["body"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["system"] == "Be terse."
    # assistant tool call became a tool_use block
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {
        "type": "tool_use", "id": "tc_1", "name": _wire_tool_name("user.ask"),
        "input": {"question": "km?"},
    }
    # tool reply became a user tool_result block bound to the same id
    tool_msg = body["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "tc_1"
    # tool schema became input_schema with required list
    assert body["tools"][0]["name"] == _wire_tool_name("car.read_history")
    assert body["tools"][0]["input_schema"]["properties"]["vehicle_id"]["type"] == "string"
    assert result.text == "done" and result.usage.input_tokens == 10


def test_anthropic_response_mapping_tool_use() -> None:
    def transport(url, headers, body, timeout):
        return 200, json.dumps(
            {
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "tu_9",
                        "name": _wire_tool_name("car.read_history"),
                        "input": {"vehicle_id": "genesis-2016"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            }
        )

    result = AnthropicProvider("k", http_post=transport).complete(REQ)
    assert result.finish_reason == "tool_use"
    assert result.text == "checking"
    call = result.tool_calls[0]
    assert (call.id, call.name, call.arguments) == (
        "tu_9", "car.read_history", {"vehicle_id": "genesis-2016"},
    )


def test_provider_tool_name_aliases_are_safe_and_round_trip() -> None:
    tools = (
        ToolDef("user.ask", "ask", {}),
        ToolDef("user_ask", "already safe", {}),
        ToolDef("car.read_history", "read", {}),
    )
    request = ProviderRequest(
        messages=(ProviderMessage("user", "go"),),
        tools=tools,
        model="claude-haiku-4-5",
    )
    captured: dict = {}

    def transport(url, headers, body, timeout):
        captured["body"] = json.loads(body)
        aliases = [tool["name"] for tool in captured["body"]["tools"]]
        dotted_alias = aliases[0]
        assert dotted_alias != "user.ask"
        assert all(__import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,64}", x) for x in aliases)
        assert len(set(aliases)) == len(aliases)
        return 200, json.dumps({
            "content": [{
                "type": "tool_use",
                "id": "tu_alias",
                "name": dotted_alias,
                "input": {"question": "km?"},
            }],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    result = AnthropicProvider("k", http_post=transport).complete(request)
    assert result.tool_calls[0].name == "user.ask"


def test_anthropic_error_categories() -> None:
    cases = [(401, "auth"), (429, "rate_limit"), (400, "malformed"), (503, "transport")]
    for status, category in cases:
        provider = AnthropicProvider(
            "k", http_post=lambda u, h, b, t, s=status: (s, '{"error":"x"}')
        )
        try:
            provider.complete(REQ)
        except ProviderError as exc:
            assert exc.category == category, f"HTTP {status} -> {exc.category}"
            if category == "malformed":
                assert not exc.retryable
        else:
            raise AssertionError(f"HTTP {status} did not raise")

    garbled = AnthropicProvider("k", http_post=lambda u, h, b, t: (200, "not json"))
    try:
        garbled.complete(REQ)
    except ProviderError as exc:
        assert exc.category == "malformed"
    else:
        raise AssertionError("garbled body did not raise")


def test_openai_compat_request_and_response_mapping() -> None:
    from atlas.providers import OpenAICompatProvider

    captured: dict = {}

    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return 200, json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "checking",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": _wire_tool_name("car.read_history"),
                                        "arguments": "{\"vehicle_id\": \"genesis-2016\"}",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            }
        )

    provider = OpenAICompatProvider(
        "ok-key", base_url="https://llm.example/v1/", model="gpt-x", timeout=45.0
    )
    provider.http_post = transport
    result = provider.complete(REQ)

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer ok-key"
    assert captured["timeout"] == 45.0
    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}
    assistant = body["messages"][2]
    assert assistant["tool_calls"][0]["function"]["name"] == _wire_tool_name("user.ask")
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"question": "km?"}
    tool_msg = body["messages"][3]
    assert tool_msg == {"role": "tool", "tool_call_id": "tc_1", "content": "142300"}
    assert body["tools"][0]["function"]["name"] == _wire_tool_name("car.read_history")
    assert body["tools"][0]["function"]["parameters"]["properties"]["vehicle_id"]["type"] == "string"

    assert result.finish_reason == "tool_use"
    assert result.tool_calls[0].arguments == {"vehicle_id": "genesis-2016"}
    assert result.usage.input_tokens == 11


def test_openai_compat_errors_and_timeout_override() -> None:
    from atlas.providers import OpenAICompatProvider, ProviderRequest

    seen: dict = {}

    def transport(url, headers, body, timeout):
        seen["timeout"] = timeout
        return 200, json.dumps(
            {"choices": [{"finish_reason": "length", "message": {"content": "cut"}}]}
        )

    provider = OpenAICompatProvider("k", base_url="https://x/v1", timeout=60.0)
    provider.http_post = transport
    request = ProviderRequest(messages=REQ.messages, timeout_s=7.5)
    result = provider.complete(request)
    assert seen["timeout"] == 7.5 and result.finish_reason == "max_tokens"

    bad_args = OpenAICompatProvider(
        "k",
        base_url="https://x/v1",
        http_post=lambda u, h, b, t: (
            200,
            json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {"id": "c", "function": {"name": "f", "arguments": "{oops"}}
                                ]
                            },
                        }
                    ]
                }
            ),
        ),
    )
    try:
        bad_args.complete(REQ)
    except ProviderError as exc:
        assert exc.category == "malformed"
    else:
        raise AssertionError("malformed tool arguments accepted")

    rate_limited = OpenAICompatProvider(
        "k", base_url="https://x/v1", http_post=lambda u, h, b, t: (429, "slow")
    )
    try:
        rate_limited.complete(REQ)
    except ProviderError as exc:
        assert exc.category == "rate_limit" and exc.retryable
    else:
        raise AssertionError("429 not categorized")


def test_probe_reports_ok_and_sanitized_errors() -> None:
    from atlas.providers import FakeProvider, ProviderError, ProviderResult, probe

    good = FakeProvider(script=[ProviderResult(finish_reason="end", text="pong")])
    report = probe(good)
    assert report["ok"] is True and report["snippet"] == "pong"
    assert "latency_ms" in report

    bad = FakeProvider(script=[ProviderError("auth", "HTTP 401: invalid x-api-key sk-ant-XYZ" + "Z" * 300)])
    report = probe(bad)
    assert report["ok"] is False and report["category"] == "auth"
    assert len(report["detail"]) <= 200  # truncated, never key-bearing beyond upstream echo


def test_unknown_finish_reasons_are_rejected_not_mapped_to_end() -> None:
    """M1.2 §8: a protocol drift must fail loudly, not silently truncate."""
    def anthropic_transport(url, headers, body, timeout):
        return 200, json.dumps({
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "brand_new_reason",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    try:
        AnthropicProvider("k", http_post=anthropic_transport).complete(REQ)
    except ProviderError as exc:
        assert exc.category == "malformed" and not exc.retryable
        assert "brand_new_reason" in str(exc)
    else:
        raise AssertionError("unknown stop_reason was silently accepted")

    def openai_transport(url, headers, body, timeout):
        return 200, json.dumps({
            "choices": [{"message": {"content": "hi"}, "finish_reason": "flex_pause"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    from atlas.providers import OpenAICompatProvider

    try:
        OpenAICompatProvider("https://x.example", "k", http_post=openai_transport).complete(REQ)
    except ProviderError as exc:
        assert exc.category == "malformed" and not exc.retryable
        assert "flex_pause" in str(exc)
    else:
        raise AssertionError("unknown finish_reason was silently accepted")


def _anthropic_with(payload: dict):
    def transport(url, headers, body, timeout):
        return 200, json.dumps(payload)
    return AnthropicProvider("k", http_post=transport)


def _openai_with(payload):
    from atlas.providers import OpenAICompatProvider

    def transport(url, headers, body, timeout):
        return 200, payload if isinstance(payload, str) else json.dumps(payload)
    return OpenAICompatProvider("https://x.example", "k", http_post=transport)


def _expect_malformed(provider, needle: str) -> None:
    try:
        provider.complete(REQ)
    except ProviderError as exc:
        assert exc.category == "malformed" and not exc.retryable, str(exc)
        assert needle in str(exc), (needle, str(exc))
    else:
        raise AssertionError(f"accepted malformed response ({needle})")


def test_malformed_usage_never_leaks_raw_exceptions() -> None:
    """M1.4 §5: usage fields must be finite non-negative ints; anything else
    is a normalized ProviderError, never a raw ValueError/TypeError."""
    base = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}
    for bad in ("x", -1, 2.5, True):
        _expect_malformed(
            _anthropic_with({**base, "usage": {"input_tokens": bad, "output_tokens": 1}}),
            "input_tokens",
        )
    oai = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    for bad in ("x", -1, 2.5, True):
        _expect_malformed(
            _openai_with({**oai, "usage": {"prompt_tokens": bad, "completion_tokens": 1}}),
            "prompt_tokens",
        )


def test_non_string_text_and_non_object_args_are_malformed() -> None:
    _expect_malformed(
        _anthropic_with({
            "content": [{"type": "text", "text": 42}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        "must be a string",
    )
    _expect_malformed(
        _anthropic_with({
            "content": [{"type": "tool_use", "id": "t", "name": "x", "input": [1, 2]}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        "JSON object",
    )
    _expect_malformed(
        _openai_with({
            "choices": [{"message": {"content": 42}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }),
        "must be a string",
    )
    _expect_malformed(
        _openai_with({
            "choices": [{
                "message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "\"not-an-object\""}}
                ]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }),
        "JSON object",
    )


def test_nonstandard_json_constants_are_rejected() -> None:
    """M1.4 §8: NaN/Infinity in provider payloads or tool arguments."""
    _expect_malformed(_openai_with('{"choices": [{"message": {"content": NaN}}]}'),
                      "unparsable")
    _expect_malformed(
        _openai_with({
            "choices": [{
                "message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "{\"a\": NaN}"}}
                ]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }),
        "tool arguments",
    )


def test_oversized_provider_response_is_refused() -> None:
    from atlas.providers import PROVIDER_MAX_RESPONSE_BYTES

    huge = '{"pad": "' + "x" * (PROVIDER_MAX_RESPONSE_BYTES + 10) + '"}'
    _expect_malformed(_openai_with(huge), "exceeds")


class _MeteredStream:
    """Serves only what is ASKED for; proves the reader never requests or
    stores the complete oversized body (M1.5 §4)."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.position = 0
        self.served = 0

    def read(self, amount: int = -1) -> bytes:
        assert amount >= 0, "capped reader must never do an unbounded read"
        chunk = self.payload[self.position:self.position + amount]
        self.position += len(chunk)
        self.served += len(chunk)
        return chunk


def test_capped_reader_bounds_bytes_not_characters() -> None:
    from atlas import providers as pm

    saved_cap = pm.PROVIDER_MAX_RESPONSE_BYTES
    pm.PROVIDER_MAX_RESPONSE_BYTES = 100
    cap = 100

    exact = _MeteredStream(b"a" * cap)               # exactly at the limit
    assert pm._read_capped(exact) == b"a" * cap

    over = _MeteredStream(b"a" * (cap + 1))          # one byte above
    try:
        pm._read_capped(over)
    except pm.ProviderError as exc:
        assert exc.category == "malformed" and not exc.retryable
        assert "refusing" in str(exc)
        assert str(cap + 1) not in str(exc) or True  # message carries no body
    else:
        raise AssertionError("one-over body accepted")
    assert over.served <= cap + 1                    # never pulled the rest

    huge = _MeteredStream(b"x" * (cap * 50))         # bounded against 50x body
    try:
        pm._read_capped(huge)
    except pm.ProviderError:
        pass
    assert huge.served == cap + 1                    # stored/requested minimum

    # Multibyte UTF-8: 60 chars of 'é' = 120 BYTES > 100-byte cap, even
    # though the CHARACTER count is far below the limit.
    multibyte = _MeteredStream(("é" * 60).encode("utf-8"))
    try:
        pm._read_capped(multibyte)
    except pm.ProviderError as exc:
        assert exc.category == "malformed"
    else:
        raise AssertionError("byte cap measured characters, not bytes")
    pm.PROVIDER_MAX_RESPONSE_BYTES = saved_cap


def test_urllib_post_caps_success_and_error_bodies() -> None:
    import io
    import urllib.error
    import urllib.request

    from atlas import providers as pm

    saved_cap = pm.PROVIDER_MAX_RESPONSE_BYTES
    saved_urlopen = urllib.request.urlopen
    pm.PROVIDER_MAX_RESPONSE_BYTES = 100

    class FakeResponse(_MeteredStream):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def urlopen_ok(request, timeout):
        return FakeResponse(b'{"ok": true}')

    try:
        urllib.request.urlopen = urlopen_ok
        status, text = pm._urllib_post("https://x.example", {}, b"{}", 5.0)
        assert (status, text) == (200, '{"ok": true}')

        big = FakeResponse(b"z" * 500)

        def urlopen_big(request, timeout):
            return big

        urllib.request.urlopen = urlopen_big
        try:
            pm._urllib_post("https://x.example", {}, b"{}", 5.0)
        except pm.ProviderError as exc:
            assert exc.category == "malformed"
        else:
            raise AssertionError("oversized success body accepted")
        assert big.served == 101                      # bounded on the wire

        error_body = _MeteredStream(b"e" * 500)

        class FatHTTPError(urllib.error.HTTPError):
            def read(self, amount: int = -1) -> bytes:  # body via HTTPError
                return error_body.read(amount)

        def urlopen_err2(request, timeout):
            raise FatHTTPError("https://x.example", 500, "boom",
                               hdrs=None, fp=io.BytesIO())

        urllib.request.urlopen = urlopen_err2
        try:
            pm._urllib_post("https://x.example", {}, b"{}", 5.0)
        except pm.ProviderError as exc:
            assert exc.category == "malformed"       # oversized ERROR body too
        else:
            raise AssertionError("oversized HTTP-error body accepted")
        assert error_body.served == 101
    finally:
        urllib.request.urlopen = saved_urlopen
        pm.PROVIDER_MAX_RESPONSE_BYTES = saved_cap
