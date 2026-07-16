"""Production ASGI entrypoint (M1.4 §7).

Importing atlas.app must be side-effect-free — no database creation, no
migrations, no preflight — so tests and library callers can
`from atlas.app import create_app` safely. THIS module is where the real
application instance is constructed for uvicorn:

    uvicorn atlas.asgi:app
"""
from atlas.app import create_app

app = create_app()
