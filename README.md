# mimir-climate

The home's thermostats for an assistant. An MCP server that reads every thermostat (temperature, humidity, mode,
setpoints, heating or cooling) and changes a schedule period's setpoints when the person asks. OpenClaw's Personal
agent uses it through the `mimir-climate` skill in [mimir-claw](https://github.com/shrshk/mimir-claw).

Made for Lennox iComfort M30 thermostats. The M30 has no local API, and Lennox's cloud API now answers only Lennox's
own app ([lennoxs30api#101](https://github.com/PeteRager/lennoxs30api/issues/101)). So mimir-climate uses the
lennoxicomfort.com web portal, as a person would, with headless Chromium (Playwright): it logs in, opens a system,
reads the page, and saves a change with the page's own save call. It then reads the schedule again to check that the
portal kept the change.

## Tools

| Tool | What it does |
|---|---|
| `status` | Every system now: temperature, humidity, heating/cooling/idle, mode or schedule, heat-to and cool-to |
| `schedule` | One system's schedule: periods (start, heat, cool, fan) and the limits (range, deadband) |
| `set_schedule_period` | Change one period's heat and/or cool setpoint (all days); refused outside the limits |

A system in AWAY shows no schedules on the portal: end Away on the thermostat or in the Lennox app first.

## Set up (on the Mac that runs OpenClaw)

1. `brew install uv`.
2. Copy `settings.example.json` to `~/.config/mimir-climate/settings.json` and fill it in:
   - `systems`: names for the portal's system tiles, in the order the portal shows them;
   - `secret_command`, `username`, `password`: a command that prints a secret given a reference, and the references
     for the portal login (for 1Password: `["op", "read"]` and `op://…` references). The login is read at each
     login and never stored, logged or returned.
3. `bash scripts/install-mac.sh`. It installs the service and its headless browser, and starts it at login
   (launchd). On first start it makes `~/.config/mimir-climate/mcp-token`, which OpenClaw sends as a bearer token.

The server listens on 127.0.0.1:8725 only and refuses calls without the token or through a proxy.

## Develop

```
cd service
uv run --group dev pytest
```

The tests use a stand-in for the portal; nothing reaches Lennox.
