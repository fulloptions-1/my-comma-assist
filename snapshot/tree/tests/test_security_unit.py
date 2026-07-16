"""Pure security-primitive tests (matrix L1/L3 kernel side).

The FastAPI wiring in atlas/app.py cannot execute in the offline review
sandbox; HTTP-level verification commands live in docs/FABLE5_CHANGELOG.md.
"""
from atlas.security import RateLimiter, parse_bearer, token_matches


def test_parse_bearer_variants() -> None:
    assert parse_bearer("Bearer abc123") == "abc123"
    assert parse_bearer("bearer abc123") == "abc123"  # scheme is case-insensitive
    assert parse_bearer("Bearer   spaced   ") == "spaced"
    assert parse_bearer(None) is None
    assert parse_bearer("") is None
    assert parse_bearer("Basic abc123") is None
    assert parse_bearer("Bearer") is None
    assert parse_bearer("Bearer ") is None


def test_token_matches_semantics() -> None:
    assert token_matches("secret-token", "secret-token") is True
    assert token_matches("wrong", "secret-token") is False
    assert token_matches("", "secret-token") is False
    assert token_matches(None, "secret-token") is False
    # empty expected token can never authorize anything
    assert token_matches("anything", "") is False
    assert token_matches("", "") is False


def test_rate_limiter_window_and_reset() -> None:
    clock = {"now": 100.0}
    limiter = RateLimiter(limit=3, window_s=60.0, clock=lambda: clock["now"])

    assert [limiter.allow("ip-a") for _ in range(3)] == [True, True, True]
    assert limiter.allow("ip-a") is False  # fourth call in window denied
    assert limiter.retry_after("ip-a") == 60.0 - 0.0 or limiter.retry_after("ip-a") <= 60.0

    # other clients are independent
    assert limiter.allow("ip-b") is True

    # window rolls over -> counter resets
    clock["now"] = 161.0
    assert limiter.allow("ip-a") is True

    # partial elapse keeps denying and reports remaining wait
    clock["now"] = 170.0
    limiter.allow("ip-a")
    limiter.allow("ip-a")
    assert limiter.allow("ip-a") is False
    remaining = limiter.retry_after("ip-a")
    assert 0.0 < remaining <= 60.0


def test_rate_limiter_rejects_bad_config() -> None:
    for limit, window in ((0, 60.0), (5, 0.0), (-1, 10.0)):
        try:
            RateLimiter(limit=limit, window_s=window)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted limit={limit}, window={window}")
