import json

from fastapi.testclient import TestClient

from mimir_climate.app import create_app, mcp_token
from mimir_climate.portal import PortalError

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeThermostats:
    """Stands in for the maker's portal (the process edge): records changes, answers like the portal."""

    def __init__(self):
        self.periods = {"upstairs": {0: [67, 80], 3: [66, 84]}}
        self.changes = []

    async def status(self):
        return [{"system": "upstairs", "temperature": 75, "mode": "Schedule IQ", "heat_to": 67, "cool_to": 80}]

    async def schedule(self, system):
        if system not in self.periods:
            raise PortalError(f"no system {system!r}; the systems are upstairs")
        return {
            "system": system,
            "periods": [{"period_id": k, "heat": h, "cool": c} for k, (h, c) in self.periods[system].items()],
        }

    async def set_period(self, system, period_id, heat, cool):
        self.changes.append((system, period_id, heat, cool))
        p = self.periods[system][period_id]
        p[:] = [heat if heat is not None else p[0], cool if cool is not None else p[1]]
        return {"system": system, "period": {"period_id": period_id, "heat": p[0], "cool": p[1]}, "changed": True}


def setup():
    fake = FakeThermostats()
    return TestClient(create_app(fake, TOKEN), base_url="http://127.0.0.1:8725"), fake


def rpc(c, method, params=None, headers=AUTH):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


def tool(c, name, **args):
    r = rpc(c, "tools/call", {"name": name, "arguments": args}).json()["result"]
    text = r["content"][0]["text"]
    return (text, True) if r["isError"] else (json.loads(text), False)


def test_only_the_token_holder_on_this_mac_gets_in():
    c, _ = setup()
    assert rpc(c, "ping", headers={}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert rpc(c, "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert rpc(c, "ping", headers=AUTH | {"X-Forwarded-For": "100.64.0.2"}).status_code == 403


def test_a_client_starts_up_and_lists_the_tools():
    c, _ = setup()
    init = rpc(c, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["name"] == "mimir-climate"
    names = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
    assert names == ["status", "schedule", "set_schedule_period"]


def test_status_and_schedule_read_through():
    c, _ = setup()
    got, failed = tool(c, "status")
    assert not failed and got[0]["mode"] == "Schedule IQ"
    got, failed = tool(c, "schedule", system="upstairs")
    assert not failed and got["periods"][1] == {"period_id": 3, "heat": 66, "cool": 84}


def test_a_portal_failure_comes_back_as_a_tool_error():
    c, _ = setup()
    text, failed = tool(c, "schedule", system="attic")
    assert failed and "the systems are upstairs" in text


def test_setting_one_setpoint_keeps_the_other():
    c, fake = setup()
    got, failed = tool(c, "set_schedule_period", system="upstairs", period_id=0, heat=72)
    assert not failed
    assert fake.changes == [("upstairs", 0, 72, None)]
    assert got["period"] == {"period_id": 0, "heat": 72, "cool": 80}


def test_period_zero_counts_as_given():
    c, fake = setup()
    _, failed = tool(c, "set_schedule_period", system="upstairs", period_id=0, cool=82)
    assert not failed and fake.changes == [("upstairs", 0, None, 82)]


def test_a_change_without_setpoints_or_with_a_fraction_is_refused_before_the_portal():
    c, fake = setup()
    text, failed = tool(c, "set_schedule_period", system="upstairs", period_id=0)
    assert failed and "give heat, cool or both" in text
    text, failed = tool(c, "set_schedule_period", system="upstairs", period_id=0, heat=71.5)
    assert failed and "whole number" in text
    text, failed = tool(c, "set_schedule_period", system="upstairs", heat=72)
    assert failed and "missing period_id" in text
    assert fake.changes == []


def test_the_token_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "cfg" / "mcp-token"
    first = mcp_token(path)
    assert len(first) >= 40 and mcp_token(path) == first
    assert path.stat().st_mode & 0o077 == 0
