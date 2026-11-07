"""Thermostat providers: one module each, chosen by the settings' "provider".

To add one: write providers/<name>.py with a make(settings, names) function that returns an object with the calls in
mimir_climate.thermostats (status, schedule, set_period, close), put its own dependencies in an optional extra of
the same name in pyproject.toml, and list it in PROVIDERS. Its settings live under its name in settings.json.
"""

import importlib
from typing import Any

from mimir_climate.thermostats import Thermostats

PROVIDERS = {
    "lennox": "Lennox iComfort thermostats (M30 and others on lennoxicomfort.com), through the web portal",
}


def load(cfg: dict[str, Any]) -> Thermostats:
    name = cfg.get("provider")
    if name not in PROVIDERS:
        known = ", ".join(f"{k} ({v})" for k, v in PROVIDERS.items())
        raise SystemExit(f"set provider in settings.json to one of: {known}")
    try:
        module = importlib.import_module(f"mimir_climate.providers.{name}")
    except ImportError as e:
        raise SystemExit(f"the {name} provider needs its extra: install mimir-climate[{name}] ({e.name} missing)")
    return module.make(cfg.get(name) or {}, cfg.get("systems") or [])
