"""The thermostats, through their maker's web portal, driven with headless Chromium.

The iComfort M30 has no local API, and the maker's cloud API now answers only the maker's own app
(lennoxs30api#101). The web portal still works, so this uses it as a person would: it logs in, opens a system's tile
and reads the page. A change sends the page's own save call (UpdateScheduleConfig) through the page's jQuery, the way
its sliders do, then opens the editor again to check that the portal kept it.

One browser session is kept between calls and used by one call at a time. The login is read from the secret store at
each login and is never kept, logged or returned.
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

BASE = "https://www.lennoxicomfort.com"
TILES = ".bxslider.HomeZones > *:not(.bx-clone)"
WAIT_MS = 45_000
NAV_MS = 90_000
READ_BACK_WAITS = (2, 15, 30)  # seconds before each check that a change was kept
ALL_DAYS = "ALL_SEVEN_DAYS"

log = logging.getLogger("mimir_climate")

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


class PortalError(Exception):
    """A failure the person can act on. The message is safe to show: it never holds the login."""


def parse_status(text: str) -> dict[str, Any]:
    """A system's page text to its state."""
    m = STATUS.search(" ".join(text.split()))
    if not m:
        raise PortalError("could not read the system's page")
    g = m.groupdict()
    return {
        "temperature": int(g["temp"]),
        "humidity": int(g["humidity"]) if g["humidity"] else None,
        "running": g["running"] or "idle",
        "mode": g["mode"],
        "heat_to": int(g["heat"]) if g["heat"] else None,
        "cool_to": int(g["cool"]) if g["cool"] else None,
    }


def check_setpoints(heat: int, cool: int, limits: dict[str, Any]) -> None:
    """Refuse what the thermostat would refuse: setpoints out of range, or heat and cool too close together."""
    for name, value in (("heat", heat), ("cool", cool)):
        lo, hi = limits[name]
        if not lo <= value <= hi:
            raise PortalError(f"{name} {value} is outside {lo}-{hi}")
    if cool - heat < limits["deadband"]:
        raise PortalError(f"cool must be at least {limits['deadband']} above heat (asked heat {heat}, cool {cool})")


def system_names(count: int, names: Sequence[str]) -> list[str]:
    """The portal's tiles in order, named from the settings ("system 3" for a tile without a name)."""
    return [names[i] if i < len(names) else f"system {i + 1}" for i in range(count)]


def find_period(schedule: dict[str, Any], period_id: int) -> dict[str, Any]:
    for p in schedule["periods"]:
        if p["period_id"] == period_id:
            return p
    ids = ", ".join(str(p["period_id"]) for p in schedule["periods"])
    raise PortalError(f"no period {period_id}; the periods are {ids}")


class Portal:
    def __init__(self, secret_command: Sequence[str], username_ref: str, password_ref: str, names: Sequence[str]):
        self._secret = list(secret_command)
        self._refs = (username_ref, password_ref)
        self._names = list(names)
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    # -- the tools' calls -------------------------------------------------------------------------------------

    async def status(self) -> list[dict[str, Any]]:
        return await self._run(self._status)

    async def schedule(self, system: str) -> dict[str, Any]:
        return await self._run(self._schedule, system)

    async def set_period(self, system: str, period_id: int, heat: int | None, cool: int | None) -> dict[str, Any]:
        return await self._run(self._set_period, system, period_id, heat, cool)

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
            raise PortalError("the portal did not answer as expected; try again in a few minutes")

    def _read_secret(self, ref: str) -> str:
        try:
            out = subprocess.run([*self._secret, ref], capture_output=True, text=True, check=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise PortalError("could not read the portal login from the secret store") from None
        if not out.stdout.strip():
            raise PortalError("the secret store gave an empty portal login")
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
                    raise PortalError("the portal refused the login; check it in the secret store") from None
                await pg.reload(wait_until="domcontentloaded")
        raise PortalError("the portal did not show the systems; try again in a few minutes")

    async def _open_system(self, pg: Page, system: str) -> str:
        """Opens a system's page (the portal then acts on that system); its text."""
        names = await self._home(pg)
        if system not in names:
            raise PortalError(f"no system {system!r}; the systems are {', '.join(names)}")
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
            raise PortalError(
                f"{system} is in {mode}: the portal hides its schedules until that ends "
                "(end it on the thermostat or in the maker's app)"
            )
        await menu.first.click()
        await pg.wait_for_load_state("networkidle")
        await pg.get_by_text("schedules", exact=True).locator("visible=true").first.click()
        await pg.wait_for_load_state("networkidle")
        await pg.get_by_text("edit schedules").locator("visible=true").first.click()
        await pg.wait_for_selector("td.slider", timeout=WAIT_MS)
        await asyncio.sleep(1)
        return await pg.evaluate(SCHEDULE_JS)

    # -- the work ---------------------------------------------------------------------------------------------

    async def _status(self, pg: Page) -> list[dict[str, Any]]:
        out = []
        for system in await self._home(pg):
            out.append({"system": system} | parse_status(await self._open_system(pg, system)))
        return out

    async def _schedule(self, pg: Page, system: str) -> dict[str, Any]:
        return {"system": system} | await self._open_editor(pg, system)

    async def _set_period(
        self, pg: Page, system: str, period_id: int, heat: int | None, cool: int | None
    ) -> dict[str, Any]:
        before = await self._open_editor(pg, system)
        if before["days"] != ALL_DAYS:
            raise PortalError(f"{system}'s schedule has separate days ({before['days']}); change it on the thermostat")
        was = find_period(before, period_id)
        heat = was["heat"] if heat is None else heat
        cool = was["cool"] if cool is None else cool
        check_setpoints(heat, cool, before["limits"])
        out = {"system": system, "schedule": before["schedule"], "before": {"heat": was["heat"], "cool": was["cool"]}}
        if (heat, cool) == (was["heat"], was["cool"]):
            return out | {"period": was, "changed": False}
        body = {
            "scheduleId": before["schedule_id"],
            "isPeriodUpdated": True,
            "periodUpdateConfig": {
                "periodID": period_id,
                "isSetPointsUpdated": True,
                "csp": cool,
                "hsp": heat,
                "isSSPOn": False,
            },
            "CurrentDaysMode": ALL_DAYS,
        }
        if await pg.evaluate(SAVE_JS, body) != "success":
            raise PortalError("the portal refused the change")
        for wait in READ_BACK_WAITS:  # read back on a fresh page; the portal can take a little while to show it
            await asyncio.sleep(wait)
            now = find_period(await self._open_editor(pg, system), period_id)
            if (now["heat"], now["cool"]) == (heat, cool):
                break
        else:
            raise PortalError(f"the portal kept heat {now['heat']} / cool {now['cool']} instead")
        return out | {"period": now, "changed": True}
