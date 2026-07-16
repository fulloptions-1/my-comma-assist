#!/usr/bin/env python3
"""Execute atlas/app.py end-to-end with STUBBED fastapi/pydantic (M1.2 §7).

The kernel test environment may not have FastAPI installed. This check
substitutes minimal stand-ins so the module actually EXECUTES — preflight,
Database + migrations, Runtime + registry, route registration, and the
module-level `app = create_app()` — instead of only being parsed. It also
verifies the factory's production refusal without secrets.

This is a structural smoke check, NOT a substitute for tests/test_http_api.py,
which runs the real TestClient flows where FastAPI is installed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    fastapi = types.ModuleType("fastapi")

    class _State:  # attribute bag
        pass

    class FastAPI:
        def __init__(self, **kwargs):
            self.state = _State()
            self.routes = []

        def _register(self, path):
            def decorator(fn):
                self.routes.append(path)
                return fn
            return decorator

        def get(self, path, **k): return self._register(path)
        def post(self, path, **k): return self._register(path)
        def put(self, path, **k): return self._register(path)
        def delete(self, path, **k): return self._register(path)
        def middleware(self, kind): return lambda fn: fn
        def add_middleware(self, cls, **kwargs): pass
        def mount(self, *a, **k): pass

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            self.status_code, self.detail = status_code, detail

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Request = object
    fastapi.Response = object
    fastapi.Depends = lambda fn: fn
    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = lambda *a, **k: None
    responses.FileResponse = lambda *a, **k: None
    fastapi.responses = responses
    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = lambda **k: None
    fastapi.staticfiles = staticfiles
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules["fastapi.staticfiles"] = staticfiles

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init_subclass__(cls, **k):
            pass

    pydantic.BaseModel = BaseModel
    pydantic.ConfigDict = dict
    pydantic.Field = lambda *a, **k: None
    pydantic.StrictBool = bool
    pydantic.StrictInt = int
    pydantic.StrictFloat = float
    pydantic.field_validator = lambda *a, **k: (lambda fn: fn)
    pydantic.BeforeValidator = lambda fn: fn
    sys.modules["pydantic"] = pydantic


def main() -> None:
    _install_stubs()
    tmp = tempfile.mkdtemp()
    from cryptography.fernet import Fernet  # real key: SecretStore validates it
    fernet_key = Fernet.generate_key().decode()
    os.environ.update(
        ATLAS_ENV="development",
        ATLAS_DB_PATH=os.path.join(tmp, "atlas.db"),
        ATLAS_ACCESS_TOKEN="check-token",
        ATLAS_SECRET_KEY=fernet_key,
    )
    import atlas.app as appmod  # must be SIDE-EFFECT-FREE (M1.4 §7)

    assert not hasattr(appmod, "app"), "atlas.app must not build an instance at import"
    assert callable(appmod.create_app), "create_app factory missing"
    repo_root = Path(appmod.__file__).resolve().parent.parent
    assert not (repo_root / "atlas.db").exists(), "bare import created a database"

    import atlas.asgi as asgimod  # the entrypoint DOES construct the app

    app = asgimod.app
    assert app is not None, "atlas.asgi entrypoint missing app"
    assert "/api/runs" in app.routes, app.routes
    assert app.state.preflight.storage in (
        "mounted", "ephemeral-acknowledged", "unconfirmed"
    )

    # Factory-level production refusal without secrets.
    try:
        appmod.create_app(
            env="production", access_token="", secret_key="",
            db_path=os.path.join(tmp, "prod.db"),
        )
    except RuntimeError as exc:
        assert "refused to start" in str(exc)
    else:
        raise SystemExit("FAIL: production app built without secrets")

    # Isolated side-by-side instances (the §7 requirement).
    a = appmod.create_app(env="development", access_token="t", secret_key=fernet_key,
                          db_path=os.path.join(tmp, "a.db"))
    b = appmod.create_app(env="development", access_token="t", secret_key=fernet_key,
                          db_path=os.path.join(tmp, "b.db"))
    assert a is not b and a.state.db is not b.state.db
    print("app import check: OK (module app + factory isolation + prod refusal)")


if __name__ == "__main__":
    main()
