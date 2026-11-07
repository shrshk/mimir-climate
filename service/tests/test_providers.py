import pytest

from mimir_climate import providers
from mimir_climate.providers.lennox import Lennox


def test_an_unknown_or_missing_provider_names_the_known_ones():
    for cfg in ({}, {"provider": "nest"}):
        with pytest.raises(SystemExit, match="lennox"):
            providers.load(cfg)


def test_the_provider_gets_its_own_settings_and_the_system_names():
    cfg = {
        "provider": "lennox",
        "systems": ["upstairs"],
        "lennox": {"secret_command": ["op", "read"], "username": "op://v/i/u", "password": "op://v/i/p"},
    }
    got = providers.load(cfg)
    assert isinstance(got, Lennox) and got._names == ["upstairs"]
