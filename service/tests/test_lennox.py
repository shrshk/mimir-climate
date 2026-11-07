import pytest

from mimir_climate.providers.lennox import make, parse_status, portal_time, with_days
from mimir_climate.thermostats import ClimateError

# The text of a system's page as the portal shows it (whitespace as innerText gives it).
ON_SCHEDULE = (
    "my homes\nhome\nSystem\nSystem\n1\n2\nFeels Like\n57%\n77\no\nSchedule IQ\nheat-to\n72\ncool-to\n80\n"
    "modes / schedules\nclick here for support"
)
COOLING_MANUAL = (
    "home System 1 2 cooling Feels Like 55% 75 o cool only cool-to 74 modes / schedules click here for support"
)
AWAY = "home System 1 2 heating Feels Like 77 o AWAY heat-to 77 cool-to 80 click here for support"
OFF = "home System Feels Like 40% 70 o off modes / schedules click here for support"


def test_a_system_on_its_schedule():
    assert parse_status(ON_SCHEDULE) == {
        "temperature": 77,
        "humidity": 57,
        "running": "idle",
        "mode": "Schedule IQ",
        "heat_to": 72,
        "cool_to": 80,
    }


def test_a_system_cooling_in_a_manual_mode_has_only_a_cool_setpoint():
    s = parse_status(COOLING_MANUAL)
    assert (s["running"], s["mode"], s["heat_to"], s["cool_to"]) == ("cooling", "cool only", None, 74)


def test_away_shows_no_humidity_and_no_schedule_menu():
    s = parse_status(AWAY)
    assert (s["running"], s["humidity"], s["mode"], s["heat_to"], s["cool_to"]) == ("heating", None, "AWAY", 77, 80)


def test_the_dials_hidden_label_is_not_part_of_the_mode():
    s = parse_status("home System Feels Like 57% 77 o Dial Set Schedule IQ heat-to 72 cool-to 80 modes / schedules")
    assert (s["mode"], s["heat_to"], s["cool_to"]) == ("Schedule IQ", 72, 80)


def test_off_has_no_setpoints():
    s = parse_status(OFF)
    assert (s["mode"], s["heat_to"], s["cool_to"]) == ("off", None, None)


def test_a_page_that_is_not_a_system_is_refused():
    with pytest.raises(ClimateError):
        parse_status("my homes home click here for support")


@pytest.mark.parametrize("mins,text", [(0, "12:00 am"), (480, "08:00 am"), (735, "12:15 pm"), (1290, "09:30 pm")])
def test_start_times_are_written_the_portals_way(mins, text):
    assert portal_time(mins) == text


def test_period_ids_give_the_day_and_the_slot():
    editor = {
        "limits": {"heat": [60, 90], "cool": [60, 90], "deadband": 3, "unit": "F"},
        "periods": [{"period_id": 3}, {"period_id": 27}],
    }
    got = with_days(editor)
    assert [(p["day"], p["slot"]) for p in got["periods"]] == [(0, 3), (6, 3)]
    assert got["limits"]["start_step"] == 15


def test_a_login_is_required():
    with pytest.raises(SystemExit, match="password"):
        make({"secret_command": ["op", "read"], "username": "op://v/i/username"}, [])
