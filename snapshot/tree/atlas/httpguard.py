"""HTTP guard utilities — deliberately free of FastAPI imports (M1.4 §1/§6).

Living outside atlas/app.py means these behaviors are unit-testable in the
kernel suite (fake ASGI messages, pure functions) rather than only under a
full FastAPI install — the M1.3 chunked-body defect shipped precisely
because its only test could not run in the authoring environment.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

MAX_BODY_BYTES = 65_536

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class BodySizeLimitMiddleware:
    """Reject oversized request bodies with 413, with or without
    Content-Length (M1.4 §1).

    The M1.3 version raised an exception from a wrapped receive() while
    FastAPI was parsing the body; FastAPI converts receive-time exceptions
    into its generic HTTP 400, so the promised 413 never appeared. Now:

    - a declared Content-Length is validated up front: malformed -> 400,
      over the cap -> 413, otherwise the request passes through untouched
      (the server enforces declared-length framing, so no buffering is
      needed);
    - a request WITHOUT Content-Length is PRE-READ here, metering at most
      MAX + one chunk, BEFORE the downstream app is invoked: oversized ->
      413 sent directly by this middleware; otherwise the buffered ASGI
      messages are replayed downstream verbatim, so JSON parsing behaves
      exactly as if the middleware were absent;
    - an http.disconnect observed during the pre-read is buffered and
      replayed, preserving normal disconnect semantics;
    - memory stays bounded: reading stops the moment the cap is exceeded.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                declared = value
                break
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await self._reject(send, 400, "Bad Content-Length")
                return
            if length > self.max_bytes:
                await self._reject(send, 413)
                return
            await self.app(scope, receive, send)
            return

        # Length-less (e.g. chunked) body: pre-read and meter.
        buffered: list[dict] = []
        received = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    await self._reject(send, 413)
                    return
                if not message.get("more_body", False):
                    break

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(buffered):
                message = buffered[replay_index]
                replay_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, send, status: int, detail: str | None = None) -> None:
        body = json.dumps(
            {"detail": detail or f"Request body exceeds {self.max_bytes} bytes"}
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def validate_base_url(url: str, *, is_prod: bool) -> str:
    """Connector URL policy for the OpenAI-compatible endpoint (M1.4 §6).

    v1 is owner-controlled and single-tenant; the enforced rules keep keys
    off plaintext transports and stored URLs safe to extend with
    /chat/completions:
    - https required in production; http only for loopback hosts, and only
      outside production;
    - no embedded credentials, no query string, no fragment;
    - the port, when present, must parse and be 1..65535;
    - no control or whitespace characters anywhere;
    - the stored value is REBUILT from parsed components (scheme, host,
      port, path without trailing slash) — never a raw rstrip of input.
    Raises ValueError with a safe message (never echoes credentials).
    """
    cleaned = url.strip()
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError("base_url must not contain control or whitespace characters")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if not parts.hostname:
        raise ValueError("base_url must include a host")
    if parts.username or parts.password:
        raise ValueError("base_url must not embed credentials")
    if parts.query:
        raise ValueError("base_url must not contain a query string")
    if parts.fragment:
        raise ValueError("base_url must not contain a fragment")
    try:
        port = parts.port  # raises ValueError when out of range / non-numeric
    except ValueError as exc:
        raise ValueError("base_url port is invalid") from exc
    if port is not None and not (1 <= port <= 65_535):
        raise ValueError("base_url port is invalid")
    if parts.scheme == "http":
        if is_prod:
            raise ValueError("base_url must use https in production")
        if parts.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                "http base_url is allowed only for loopback development hosts "
                "(localhost, 127.0.0.1, ::1)"
            )
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, netloc, path, "", ""))
