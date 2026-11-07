"""Lennox iComfort thermostats, through the lennoxicomfort.com web portal, driven with headless Chromium.

The iComfort M30 has no local API, and Lennox's cloud API now answers only Lennox's own app (lennoxs30api#101). The
web portal still works, so this uses it as a person would: it logs in, opens a system's tile and reads the page. A
change sends the page's own save call (UpdateScheduleConfig) through the page's jQuery, the way its sliders do, then
opens the editor again to check that the portal kept it.

One browser session is kept between calls and used by one call at a time. The login is read from the secret store at
each login and is never kept, logged or returned.

Settings (under "lennox"): secret_command (a command that prints a secret given a reference, e.g. ["op", "read"]),
username and password (the references for the portal login).
"""

import asyncio
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from mimir_climate.thermostats import ClimateError, find_period, kept, plan, system_names

BASE = "https://www.lennoxicomfort.com"
TILES = ".bxslider.HomeZones > *:not(.bx-clone)"
WAIT_MS = 45_000
NAV_MS = 90_000
READ_BACK_WAITS = (2, 15, 30)  # seconds before each check that a change was kept
PER_DAY = 4  # periods per day: ids 0-3 for "all days", or 0-27 for seven separate days
START_STEP = 15  # the thermostat sets start times in quarter hours

log = logging.getLogger("mimir_climate.lennox")

# A system's page, as text: "[heating|cooling] Feels Like [55%] 75 o <mode> [heat-to 72] [cool-to 80] …"
STATUS = re.compile(
    r"(?:(?P<running>heating|cooling)\s+)?Feels Like\s+(?:(?P<humidity>\d+)%\s+)?(?P<temp>\d+)\s*o\s+(?:Dial Set\s+)?"
    r"(?P<mode>.+?)\s*(?:heat-to\s+(?P<heat>\d+)\s*)?(?:cool-to\s+(?P<cool>\d+)\s*)?(?:modes / schedules|click here)"
)

# The schedule editor: the schedule it shows, its periods (one row each), the limits and the days mode.
SCHEDULE_JS = """() => {
  const w = document.querySelector('#scheduleWrapper');
  const active = document.querySelector('#scheduleSelection li.activeSchedule a');
  const days = document.querySelector('a.selectDays');
  const periods = [...document.querySelectorAll('td.slider')].map(td => {
    const id = td.dataset.periodid;
    const start = document.getElementById(id + '-a');
    const fan = td.closest('tr').querySelector('img[data-fanmode]');
    return {period_id: +id, start: start ? start.innerText.trim() : null, heat: +td.dataset.hsp,
            cool: +td.dataset.csp, fan: fan ? fan.dataset.fanmode : null};
  });
  return {
    schedule_id: active ? +active.dataset.scheduleid : null,
    schedule: active ? active.innerText.trim() : null,
    days: days ? days.dataset.configDays : null,
    limits: {heat: [+w.dataset.minhsp, +w.dataset.maxhsp], cool: [+w.dataset.mincsp, +w.dataset.maxcsp],
             deadband: +w.dataset.deadband, unit: w.dataset.tempunit},
    periods,
  };
}"""

SAVE_JS = """(body) => new Promise(done => $.ajax({
  url: '/ModesSchedules/UpdateScheduleConfig', type: 'POST', data: JSON.stringify(body),
  contentType: 'application/json'
}).always((data, status) => done(status)))"""


def make(settings: dict[str, Any], names: Sequence[str]) -> "Lennox":
    missing = [k for k in ("secret_command", "username", "password") if not settings.get(k)]
    if missing:
        raise SystemExit(f'set {", ".join(missing)} under "lennox" in settings.json')
    return Lennox(settings["secret_command"], settings["username"], settings["password"], names)


def parse_status(text: str) -> dict[str, Any]:
    """A system's page text to its state."""
    m = STATUS.search(" ".join(text.split()))
    if not m:
        raise ClimateError("could not read the system's page")
    g = m.groupdict()
    return {
        "temperature": int(g["temp"]),
        "humidity": int(g["humidity"]) if g["humidity"] else None,
        "running": g["running"] or "idle",
        "mode": g["mode"],
        "heat_to": int(g["heat"]) if g["heat"] else None,
        "cool_to": int(g["cool"]) if g["cool"] else None,
    }


def portal_time(mins: int) -> str:
    """Minutes after midnight as the portal writes a start time: '09:30 pm'."""
    hour, minute = divmod(mins, 60)
    return f"{(hour % 12) or 12:02d}:{minute:02d} {'pm' if hour >= 12 else 'am'}"


def with_days(editor: dict[str, Any]) -> dict[str, Any]:
    """The editor's read, in the shared form: each period's day and slot, and the start step."""
    periods = [p | {"day": p["period_id"] // PER_DAY, "slot": p["period_id"] % PER_DAY} for p in editor["periods"]]
    return editor | {"periods": periods, "limits": editor["limits"] | {"start_step": START_STEP}}


class Lennox:
    def __init__(self, secret_command: Sequence[str], username_ref: str, password_ref: str, names: Sequence[str]):
        self._secret = list(secret_command)
        self._refs = (username_ref, password_ref)
        self._names = list(names)
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    # -- the shared calls -------------------------------------------------------------------------------------

    async def status(self) -> list[dict[str, Any]]:
        return await self._run(self._status)

    async def schedule(self, system: str) -> dict[str, Any]:
        return await self._run(self._schedule, system)

    async def set_period(
        self,
        system: str,
        period_id: int,
        heat: int | None,
        cool: int | None,
        start: str | None = None,
        every_day: bool = False,
    ) -> dict[str, Any]:
        return await self._run(self._set_period, system, period_id, heat, cool, start, every_day)

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except PlaywrightError:
            log.warning("browser_close_failed")
        self._pw = self._browser = self._page = None

    # -- the session ------------------------------------------------------------------------------------------

    async def _run(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        async with self._lock:
            for attempt in (1, 2):  # every call reads or sets absolute values, so one more try is safe
                if self._page is None:
                    self._pw = await async_playwright().start()
                    self._browser = await self._pw.chromium.launch()
                    self._page = await self._browser.new_page(viewport={"width": 1400, "height": 1000})
                    self._page.set_default_timeout(WAIT_MS)
                    self._page.set_default_navigation_timeout(NAV_MS)  # the portal's pages are slow at times
                try:
                    return await fn(self._page, *args)
                except PlaywrightError as e:  # also the timeouts
                    # The first line names the step (a selector, a wait); fill values never appear in it.
                    log.warning(
                        "portal_failed attempt=%d url=%s %s",
                        attempt,
                        self._page.url.split("?")[0],
                        str(e).splitlines()[0][:200] if str(e) else type(e).__name__,
                    )
                    await self.close()  # start clean
            raise ClimateError("the Lennox portal did not answer as expected; try again in a few minutes")

    def _read_secret(self, ref: str) -> str:
        try:
            out = subprocess.run([*self._secret, ref], capture_output=True, text=True, check=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise ClimateError("could not read the Lennox login from the secret store") from None
        if not out.stdout.strip():
            raise ClimateError("the secret store gave an empty Lennox login")
        return out.stdout.strip()

    async def _login(self, pg: Page) -> None:
        await pg.goto(f"{BASE}/Account/Login", wait_until="networkidle")
        for banner in ("#onetrust-reject-all-handler", "#onetrust-accept-btn-handler"):
            if await pg.locator(banner).is_visible():
                await pg.click(banner)
                break
        # The page also has hidden decoy inputs: fill the real ones by id.
        await pg.fill("#EmailAddress", self._read_secret(self._refs[0]))
        await pg.fill("#Password", self._read_secret(self._refs[1]))
        await pg.press("#Password", "Enter")
        await pg.wait_for_load_state("domcontentloaded")

    async def _home(self, pg: Page) -> list[str]:
        """My homes with its system tiles shown (logging in when the session has lapsed); the systems' names."""
        await pg.goto(f"{BASE}/Dashboard/MyHomes", wait_until="domcontentloaded")
        if "/Account/Login" in pg.url:
            await self._login(pg)
        for _ in range(3):
            try:
                await pg.wait_for_selector(f"{TILES} a", timeout=WAIT_MS)
                return system_names(await pg.locator(TILES).count(), self._names)
            except PlaywrightTimeout:
                if "/Account/Login" in pg.url:
                    raise ClimateError("the Lennox portal refused the login; check it in the secret store") from None
                await pg.reload(wait_until="domcontentloaded")
        raise ClimateError("the Lennox portal did not show the systems; try again in a few minutes")

    async def _open_system(self, pg: Page, system: str) -> str:
        """Opens a system's page (the portal then acts on that system); its text."""
        names = await self._home(pg)
        if system not in names:
            raise ClimateError(f"no system {system!r}; the systems are {', '.join(names)}")
        await pg.locator(TILES).nth(names.index(system)).locator("a").first.click()
        await pg.wait_for_url("**/Dashboard/HomeDetails**", timeout=WAIT_MS)
        await pg.wait_for_function(  # the body is briefly missing while the page swaps
            "() => !!document.body && document.body.innerText.includes('Feels Like')", timeout=WAIT_MS
        )
        await pg.wait_for_load_state("networkidle")
        await asyncio.sleep(2)  # the readings fill in just after the page settles
        return await pg.evaluate("document.body.innerText")

    async def _open_editor(self, pg: Page, system: str) -> dict[str, Any]:
        text = await self._open_system(pg, system)
        menu = pg.get_by_text("modes / schedules").locator("visible=true")
        if await menu.count() == 0:
            mode = parse_status(text)["mode"]
            raise ClimateError(
                f"{system} is in {mode}: the portal hides its schedules until that ends "
                "(end it on the thermostat or in the Lennox app)"
            )
        await menu.first.click()
        await pg.wait_for_load_state("networkidle")
        await pg.get_by_text("schedules", exact=True).locator("visible=true").first.click()
        await pg.wait_for_load_state("networkidle")
        await pg.get_by_text("edit schedules").locator("visible=true").first.click()
        await pg.wait_for_selector("td.slider", timeout=WAIT_MS)
        await asyncio.sleep(1)
        return with_days(await pg.evaluate(SCHEDULE_JS))

    # -- the work ---------------------------------------------------------------------------------------------

    async def _status(self, pg: Page) -> list[dict[str, Any]]:
        out = []
        for system in await self._home(pg):
            out.append({"system": system} | parse_status(await self._open_system(pg, system)))
        return out

    async def _schedule(self, pg: Page, system: str) -> dict[str, Any]:
        editor = await self._open_editor(pg, system)
        editor.pop("schedule_id")
        return {"system": system} | editor

    async def _set_period(
        self,
        pg: Page,
        system: str,
        period_id: int,
        heat: int | None,
        cool: int | None,
        start: str | None = None,
        every_day: bool = False,
    ) -> dict[str, Any]:
        before = await self._open_editor(pg, system)
        items = plan(before, period_id, heat, cool, start, every_day)
        out = {"system": system, "schedule": before["schedule"], "days": before["days"]}
        out["before"] = [i["period"] for i in items]
        saves = []
        for i in items:
            pid = i["period"]["period_id"]
            if i["start_changed"]:
                saves.append({"periodID": pid, "isStartTimeUpdated": True, "startTime": portal_time(i["start"])})
            if i["set_changed"]:
                saves.append(
                    {"periodID": pid, "isSetPointsUpdated": True, "csp": i["cool"], "hsp": i["heat"], "isSSPOn": False}
                )
        if not saves:
            return out | {"periods": out.pop("before"), "changed": False}
        for change in saves:
            body = {
                "scheduleId": before["schedule_id"],
                "isPeriodUpdated": True,
                "periodUpdateConfig": change,
                "CurrentDaysMode": before["days"],
            }
            if await pg.evaluate(SAVE_JS, body) != "success":
                raise ClimateError("the Lennox portal refused the change")
        for wait in READ_BACK_WAITS:  # read back on a fresh page; the portal can take a little while to show it
            await asyncio.sleep(wait)
            after = await self._open_editor(pg, system)
            now = [find_period(after, i["period"]["period_id"]) for i in items]
            if all(kept(i, n) for i, n in zip(items, now)):
                break
        else:
            off = [n for i, n in zip(items, now) if not kept(i, n)]
            raise ClimateError(f"the Lennox portal did not keep the change for {len(off)} period(s): {off}")
        # A start time change can make the portal split "all days" into separate days: report what it is now.
        return out | {"days": after["days"], "periods": now, "changed": True}
