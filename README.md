# mimir-climate

The home's thermostats for an assistant. An MCP server that reads every thermostat (temperature, humidity, mode,
setpoints, heating or cooling) and changes a schedule period's setpoints when the person asks. OpenClaw's Personal
agent uses it through the `mimir-climate` skill in [mimir-claw](https://github.com/shrshk/mimir-claw).

It works with any thermostat maker through a provider. Lennox is the first one, and the only one for now.

## Providers

| Provider | Thermostats | How |
|---|---|---|
| `lennox` | Lennox iComfort (M30 and others on lennoxicomfort.com) | The web portal, with headless Chromium (Playwright) |

**lennox.** The M30 has no local API, and Lennox's cloud API now answers only Lennox's own app
([lennoxs30api#101](https://github.com/PeteRager/lennoxs30api/issues/101)). So this provider uses the
lennoxicomfort.com web portal as a person would: it logs in, opens a system, reads the page, and saves a change with
the page's own save call, then reads the schedule again to check that the portal kept it. Changing a start time
makes the portal split an "all days" schedule into seven separate days; `every_day` then changes all of them.

**Adding a provider.** Write `service/mimir_climate/providers/<name>.py` with a `make(settings, names)` function
that returns an object with `status`, `schedule`, `set_period` and `close`, in the shapes described in
[thermostats.py](service/mimir_climate/thermostats.py). Use the shared checks there (`plan` checks setpoints, the
deadband and start times before anything is sent; `kept` checks a read-back). Put its dependencies in an optional
extra named after it in `pyproject.toml`, list it in `PROVIDERS` in `providers/__init__.py`, and add its tests.
Its settings go under its name in `settings.json`.

## Tools

| Tool | What it does |
|---|---|
| `status` | Every system now: temperature, humidity, heating/cooling/idle, mode or schedule, heat-to and cool-to |
| `schedule` | One system's schedule: periods (start, heat, cool, fan) and the limits (range, deadband) |
| `set_schedule_period` | Change one period's start, heat and/or cool, on one day or `every_day`; refused outside the limits, read back after |

With Lennox, a system in AWAY shows no schedules: end Away on the thermostat or in the Lennox app first.

## Set up (on the Mac that runs OpenClaw)

1. `brew install uv`.
2. Copy `settings.example.json` to `~/.config/mimir-climate/settings.json` and fill it in:
   - `provider`: `lennox`;
   - `systems`: names for the provider's systems, in the order it lists them (for Lennox, the portal's tiles);
   - under `lennox`: `secret_command`, `username`, `password`: a command that prints a secret given a reference, and
     the references for the portal login (for 1Password: `["op", "read"]` and `op://…` references). The login is
     read at each login and never stored, logged or returned.
3. `bash scripts/install-mac.sh`. It installs the service with its provider's extra (for Lennox, also its headless
   browser), and starts it at login (launchd). On first start it makes `~/.config/mimir-climate/mcp-token`, which
   OpenClaw sends as a bearer token.

The server listens on 127.0.0.1:8725 only and refuses calls without the token or through a proxy.

## Develop

```
cd service
uv run --group dev pytest
```

The tests use a stand-in provider and saved page text; nothing reaches a thermostat maker.
