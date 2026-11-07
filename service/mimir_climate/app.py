"""mimir-climate: starts the MCP server with the settings from the config folder.

The config folder (MIMIR_CLIMATE_CONFIG, default ~/.config/mimir-climate) holds settings.json (see
settings.example.json) and mcp-token, made on first start. Neither is in git.
"""

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from mimir_climate.mcp_server import Thermostats, router
from mimir_climate.portal import Portal

DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8725,
    "systems": [],
    "secret_command": ["op", "read"],
    "username": "",
    "password": "",
}


def config_dir() -> Path:
    return Path(os.environ.get("MIMIR_CLIMATE_CONFIG", "~/.config/mimir-climate")).expanduser()


def load_settings(folder: Path) -> dict[str, Any]:
    path = folder / "settings.json"
    cfg = DEFAULTS | (json.loads(path.read_text()) if path.exists() else {})
    if not cfg["username"] or not cfg["password"]:
        raise SystemExit(f"set username and password (secret store references) in {path}")
    return cfg


def mcp_token(path: Path) -> str:
    """The assistant's token: made on first start, owner-only; the assistant's config reads it from this file."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            path.write_text(secrets.token_urlsafe(32))
        finally:
            os.umask(old)
    return path.read_text().strip()


def create_app(thermostats: Thermostats, token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close = getattr(thermostats, "close", None)
        if close:
            await close()

    app = FastAPI(title="mimir-climate", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router(thermostats, token))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    folder = config_dir()
    cfg = load_settings(folder)
    portal = Portal(cfg["secret_command"], cfg["username"], cfg["password"], cfg["systems"])
    app = create_app(portal, mcp_token(folder / "mcp-token"))
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], access_log=False)
