"""What every thermostat provider gives the MCP server, and the checks they share.

A provider (see mimir_climate.providers) is any object with these three calls. Temperatures are whole degrees in the
unit the provider reports (limits.unit). Times of day are minutes after midnight inside, and the provider's own text
(for example "09:30 pm") in what it returns.

status() -> one dict per system, in the provider's order:
    {system, temperature, humidity|None, running: heating|cooling|idle, mode, heat_to|None, cool_to|None}
schedule(system) -> the schedule the system follows:
    {system, schedule (its name), days (how the provider splits the week), limits, periods}
    limits: {heat: [lo, hi], cool: [lo, hi], deadband, unit, start_step (minutes)}
    periods: [{period_id, day, slot, start, heat, cool, fan}]; a period runs from its start until the next period of
    the same day starts. day groups periods of one day (or of every day); slot is the period's place in its day.
set_period(system, period_id, heat|None, cool|None, start|None, every_day) -> what it changed, read back:
    {system, schedule, days, before: [periods], periods: [periods], changed}

Providers raise ClimateError with a message that is safe to show to the person (never a login or a token).
"""

import re
from collections.abc import Sequence
from typing import Any, Protocol


class ClimateError(Exception):
    """A failure the person can act on. The message is safe to show."""


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

    async def close(self) -> None: ...


def check_setpoints(heat: int, cool: int, limits: dict[str, Any]) -> None:
    """Refuse what the thermostat would refuse: setpoints out of range, or heat and cool too close together."""
    for name, value in (("heat", heat), ("cool", cool)):
        lo, hi = limits[name]
        if not lo <= value <= hi:
            raise ClimateError(f"{name} {value} is outside {lo}-{hi}")
    if cool - heat < limits["deadband"]:
        raise ClimateError(f"cool must be at least {limits['deadband']} above heat (asked heat {heat}, cool {cool})")


TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s*m\.?)?\s*$", re.IGNORECASE)


def minutes(time: str) -> int:
    """'07:00 am', '9:30 pm', '9:30pm', '21:30' or '8 am' to minutes after midnight."""
    m = TIME.match(time)
    if not m:
        raise ClimateError(f"could not read the time {time!r}; give it like 9:30 pm or 21:30")
    hour, minute, half = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if minute > 59 or hour > 23 or (half and not 1 <= hour <= 12):
        raise ClimateError(f"{time!r} is not a time of day")
    if half:
        hour = hour % 12 + (12 if half == "p" else 0)
    return hour * 60 + minute


def check_start(start: str, period: dict[str, Any], periods: list[dict[str, Any]], step: int) -> int:
    """A new start time, in minutes. Refused unless it is on the provider's step and stays after the period before it
    and before the period after it in the same day (the periods of a day run in order)."""
    mins = minutes(start)
    if mins % step:
        raise ClimateError(f"start times go in steps of {step} minutes")
    day = sorted((p for p in periods if p["day"] == period["day"]), key=lambda p: p["slot"])
    i = next(i for i, p in enumerate(day) if p["period_id"] == period["period_id"])
    if i > 0 and mins <= minutes(day[i - 1]["start"]):
        raise ClimateError(f"must start after the period before it ({day[i - 1]['start']})")
    if i + 1 < len(day) and mins >= minutes(day[i + 1]["start"]):
        raise ClimateError(f"must start before the period after it ({day[i + 1]['start']})")
    return mins


def find_period(schedule: dict[str, Any], period_id: int) -> dict[str, Any]:
    for p in schedule["periods"]:
        if p["period_id"] == period_id:
            return p
    ids = ", ".join(str(p["period_id"]) for p in schedule["periods"])
    raise ClimateError(f"no period {period_id}; the periods are {ids}")


def plan(
    schedule: dict[str, Any],
    period_id: int,
    heat: int | None,
    cool: int | None,
    start: str | None,
    every_day: bool,
) -> list[dict[str, Any]]:
    """The periods a change touches and what each should become, checked before anything is sent: every period must
    fit, or nothing changes. Each item: {period (as it is), start (minutes), heat, cool, start_changed, set_changed}.
    every_day: the same slot on every day of the schedule."""
    asked = find_period(schedule, period_id)
    targets = [p for p in schedule["periods"] if p["slot"] == asked["slot"]] if every_day else [asked]
    out = []
    for p in targets:
        h = p["heat"] if heat is None else heat
        c = p["cool"] if cool is None else cool
        check_setpoints(h, c, schedule["limits"])
        s = minutes(p["start"]) if start is None else check_start(start, p, schedule["periods"], step(schedule))
        out.append(
            {
                "period": p,
                "start": s,
                "heat": h,
                "cool": c,
                "start_changed": s != minutes(p["start"]),
                "set_changed": (h, c) != (p["heat"], p["cool"]),
            }
        )
    return out


def step(schedule: dict[str, Any]) -> int:
    return schedule["limits"].get("start_step") or 1


def kept(item: dict[str, Any], now: dict[str, Any]) -> bool:
    """Whether a period read back is what plan() asked for."""
    return (minutes(now["start"]), now["heat"], now["cool"]) == (item["start"], item["heat"], item["cool"])


def system_names(count: int, names: Sequence[str]) -> list[str]:
    """The provider's systems in its order, named from the settings ("system 3" for one without a name)."""
    return [names[i] if i < len(names) else f"system {i + 1}" for i in range(count)]
