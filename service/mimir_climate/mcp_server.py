"""mimir-climate as an MCP server, so an assistant (OpenClaw on the same Mac) can read the thermostats and change their
schedules when the person asks.

One endpoint, POST /mcp (MCP's streamable HTTP transport, answered as plain JSON; no event stream). Every call needs
the bearer token from the config folder's mcp-token. The server listens on 127.0.0.1 only and turns away anything
that came through a proxy.
"""

import hmac
import json
from typing import Any, Protocol

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from mimir_climate.portal import PortalError

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first; the client's own is answered when known

TOOLS = [
    {
        "name": "status",
        "description": "Every thermostat now: system name, indoor temperature and humidity, whether it is heating, "
        "cooling or idle, its mode (a schedule's name such as 'Schedule IQ', 'cool only', 'heat only', 'off', or "
        "AWAY), and the setpoints it holds (heat_to: heat starts below it; cool_to: cooling starts above it). "
        "Degrees Fahrenheit. Takes up to a minute.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule",
        "description": "A thermostat's schedule: its name and periods (period_id, start time, heat and cool "
        "setpoints, fan), with the limits (heat and cool ranges, deadband: the least gap between heat and cool). "
        "Not available while the system is in AWAY.",
        "inputSchema": {
            "type": "object",
            "properties": {"system": {"type": "string", "description": "a name from status, e.g. upstairs"}},
            "required": ["system"],
        },
    },
    {
        "name": "set_schedule_period",
        "description": "Change one schedule period on one thermostat: its start time, heat, cool, or any of them "
        "(what is not given stays). A period runs from its start until the next period starts, so to change when "
        "the night ends, change the start of the morning period. It applies to every day and is checked by reading "
        "the schedule again. Changing a start time can make the portal split the schedule into seven separate days (period ids 0-27, four per day); then use every_day to change the same period on every day. Only when the person asks for the change. Takes up to two minutes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "system": {"type": "string"},
                "period_id": {"type": "integer", "description": "from schedule"},
                "start": {"type": "string", "description": "e.g. 9:30 pm or 21:30; quarter hours only"},
                "every_day": {
                    "type": "boolean",
                    "description": "when the schedule has separate days: the same period on all seven days",
                },
                "heat": {"type": "integer", "description": "heat starts below this"},
                "cool": {"type": "integer", "description": "cooling starts above this"},
            },
            "required": ["system", "period_id"],
        },
    },
]


class Thermostats(Protocol):
    async def status(self) -> list[dict[str, Any]]: ...

    async def schedule(self, system: str) -> dict[str, Any]: ...

    async def set_period(
        self,
        system: str,
        period_id: int,
        heat: int | None,
        cool: int | None,
        start: str | None = None,
        every_day: bool = False,
    ) -> dict[str, Any]: ...


def whole(args: dict, key: str) -> int | None:
    v = args.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise PortalError(f"{key} must be a whole number")
    return int(v)


def router(thermostats: Thermostats, token: str) -> APIRouter:
    api = APIRouter()

    async def run(name: str, args: dict) -> Any:
        if name == "status":
            return await thermostats.status()
        if name == "schedule":
            return await thermostats.schedule(str(args["system"]))
        # set_schedule_period
        heat, cool = whole(args, "heat"), whole(args, "cool")
        start = str(args["start"]) if args.get("start") not in (None, "") else None
        if heat is None and cool is None and start is None:
            raise PortalError("give start, heat, cool or any of them")
        every_day = args.get("every_day") is True
        return await thermostats.set_period(str(args["system"]), whole(args, "period_id"), heat, cool, start, every_day)

    def answer(id_, result=None, error: tuple[int, str] | None = None) -> JSONResponse:
        body = {"jsonrpc": "2.0", "id": id_}
        body |= {"error": {"code": error[0], "message": error[1]}} if error else {"result": result}
        return JSONResponse(body)

    @api.post("/mcp")
    async def mcp(request: Request):
        if request.headers.get("x-forwarded-for"):  # only callers on this Mac, never through a proxy
            return Response(status_code=403)
        sent = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            msg = await request.json()
        except ValueError:
            return answer(None, error=(-32700, "not JSON"))
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return answer(None, error=(-32600, "not a JSON-RPC request"))
        if "id" not in msg:  # a notification (initialized, cancelled): nothing to answer
            return Response(status_code=202)
        id_, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return answer(
                id_,
                {
                    "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mimir-climate", "version": "0.1.0"},
                    "instructions": "Read the home's thermostats; change a schedule only when the person asks.",
                },
            )
        if method == "ping":
            return answer(id_, {})
        if method == "tools/list":
            return answer(id_, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return answer(id_, error=(-32602, f"no tool {name}"))
            missing = [k for k in tool["inputSchema"].get("required", []) if args.get(k) in (None, "")]
            try:
                if missing:
                    raise PortalError(f"missing {', '.join(missing)}")
                result, failed = await run(name, args), False
            except PortalError as e:
                result, failed = str(e), True
            text = result if failed else json.dumps(result, ensure_ascii=False)
            return answer(id_, {"content": [{"type": "text", "text": text}], "isError": failed})
        return answer(id_, error=(-32601, f"no method {method}"))

    @api.get("/mcp")
    def no_stream():
        return Response(status_code=405, headers={"Allow": "POST"})  # no server-sent events: every answer is JSON

    return api
