"""Kernel-runnable tests for the HTTP guard layer (M1.4 §1/§6)."""
import asyncio
import json

from atlas.httpguard import BodySizeLimitMiddleware, validate_base_url

MAX = 100  # small cap keeps fixtures readable


class Downstream:
    """Fake ASGI app: consumes the body, records everything, replies 200."""

    def __init__(self):
        self.called = False
        self.body = b""
        self.saw_disconnect = False

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                return
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def run(middleware, scope, messages):
    sent = []
    queue = list(messages)

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def scope_with(headers=()):
    return {"type": "http", "headers": list(headers)}


def status_of(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def chunks(*parts, tail_more=False):
    out = []
    for i, part in enumerate(parts):
        more = i < len(parts) - 1 or tail_more
        out.append({"type": "http.request", "body": part, "more_body": more})
    return out


def test_declared_length_fast_paths():
    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    sent = run(mw, scope_with([(b"content-length", b"101")]), [])
    assert status_of(sent) == 413 and app.called is False

    sent = run(mw, scope_with([(b"content-length", b"abc")]), [])
    assert status_of(sent) == 400 and app.called is False

    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    sent = run(mw, scope_with([(b"content-length", b"5")]), chunks(b"hello"))
    assert status_of(sent) == 200 and app.body == b"hello"


def test_lengthless_body_metered_before_downstream():
    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    sent = run(mw, scope_with(), chunks(b"x" * 60, b"y" * 60))  # 120 > 100
    assert status_of(sent) == 413
    assert app.called is False  # rejected BEFORE FastAPI could see it (§1)


def test_exact_limit_passes_and_one_over_rejects():
    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    payload = b"a" * 40 + b"b" * 60  # exactly 100
    sent = run(mw, scope_with(), chunks(payload[:40], payload[40:]))
    assert status_of(sent) == 200
    assert app.body == payload  # replay fidelity: byte-identical downstream

    app2 = Downstream()
    mw2 = BodySizeLimitMiddleware(app2, max_bytes=MAX)
    sent = run(mw2, scope_with(), chunks(b"a" * 40, b"b" * 61))  # 101
    assert status_of(sent) == 413 and app2.called is False


def test_disconnect_during_preread_is_replayed():
    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    messages = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent = run(mw, scope_with(), messages)
    assert app.called is True and app.saw_disconnect is True
    assert not any(m.get("status") == 413 for m in sent)


def test_small_chunked_json_parses_downstream():
    app = Downstream()
    mw = BodySizeLimitMiddleware(app, max_bytes=MAX)
    body = json.dumps({"secret": "tok-123"}).encode()
    sent = run(mw, scope_with(), chunks(body[:5], body[5:]))
    assert status_of(sent) == 200
    assert json.loads(app.body) == {"secret": "tok-123"}


def test_base_url_matrix():
    ok = lambda url, prod=False: validate_base_url(url, is_prod=prod)
    bad = []
    for url, prod in [
        ("http://internal.corp/v1", False),
        ("http://localhost:8080/v1", True),
        ("https://user:pass@x.example/v1", False),
        ("ftp://x.example", False),
        ("https://x.example/v1?foo=bar", False),
        ("https://x.example/v1#frag", False),
        ("https://x.example:99999/v1", False),
        ("https://x.example:0/v1", False),
        ("https://x.ex\x01ample/v1", False),
        ("https://x example/v1", False),
        ("https:///v1", False),
    ]:
        try:
            ok(url, prod)
            bad.append(url)
        except ValueError:
            pass
    assert not bad, f"accepted invalid urls: {bad}"

    assert ok("https://x.example/v1/") == "https://x.example/v1"
    assert ok("https://x.example:8443/v1") == "https://x.example:8443/v1"
    assert ok("http://localhost:8080/v1") == "http://localhost:8080/v1"
    assert ok("  https://x.example  ") == "https://x.example"
