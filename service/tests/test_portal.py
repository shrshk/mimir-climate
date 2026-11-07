import pytest

from mimir_climate.portal import (
    PortalError,
    check_setpoints,
    check_start,
    find_period,
    minutes,
    parse_status,
    system_names,
)

LIMITS = {"heat": [60, 90], "cool": [60, 90], "deadband": 3, "unit": "F"}

# The text of a system's page as the portal shows it (whitespace as innerText gives it).
ON_SCHEDULE = "my homes\nhome\nSystem\nSystem\n1\n2\nFeels Like\n57%\n77\no\nSchedule IQ\nheat-to\n72\ncool-to\n80\nmodes / schedules\nclick here for support"
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
    with pytest.raises(PortalError):
        parse_status("my homes home click here for support")


def test_setpoints_inside_the_limits_with_the_deadband_pass():
    check_setpoints(72, 80, LIMITS)
    check_setpoints(77, 80, LIMITS)


@pytest.mark.parametrize(
    "heat,cool,why", [(59, 80, "heat 59 is outside"), (72, 91, "cool 91 is outside"), (78, 80, "at least 3 above")]
)
def test_setpoints_the_thermostat_would_refuse(heat, cool, why):
    with pytest.raises(PortalError, match=why):
        check_setpoints(heat, cool, LIMITS)


def test_tiles_without_a_name_get_a_number():
    assert system_names(3, ["upstairs", "downstairs"]) == ["upstairs", "downstairs", "system 3"]
    assert system_names(1, ["upstairs", "downstairs"]) == ["upstairs"]


def test_an_unknown_period_names_the_real_ones():
    schedule = {"periods": [{"period_id": 0}, {"period_id": 3}]}
    assert find_period(schedule, 3) == {"period_id": 3}
    with pytest.raises(PortalError, match="the periods are 0, 3"):
        find_period(schedule, 1)


@pytest.mark.parametrize(
    "text,mins",
    [
        ("07:00 am", 420),
        ("9:30 pm", 1290),
        ("9:30pm", 1290),
        ("21:30", 1290),
        ("8 am", 480),
        ("12:00 am", 0),
        ("12:15 pm", 735),
        ("9:30 P.M.", 1290),
    ],
)
def test_times_people_and_the_portal_write(text, mins):
    assert minutes(text) == mins


@pytest.mark.parametrize("text", ["25:00", "13:00 pm", "9:75", "noon", ""])
def test_times_that_are_not_times(text):
    with pytest.raises(PortalError):
        minutes(text)


DAY_NIGHT = [{"period_id": 0, "start": "07:00 am"}, {"period_id": 3, "start": "11:00 pm"}]


def test_a_new_start_is_written_the_portals_way():
    assert check_start("21:30", 3, DAY_NIGHT) == "09:30 pm"
    assert check_start("8:00 am", 0, DAY_NIGHT) == "08:00 am"


@pytest.mark.parametrize(
    "start,pid,why",
    [
        ("9:40 pm", 3, "steps of 15"),
        ("6:30 am", 3, "after the period before"),
        ("11:30 pm", 0, "before the period after"),
    ],
)
def test_a_start_out_of_order_or_off_the_quarter_hour_is_refused(start, pid, why):
    with pytest.raises(PortalError, match=why):
        check_start(start, pid, DAY_NIGHT)


def test_with_separate_days_a_start_is_checked_against_its_own_day_only():
    week = [{"period_id": d * 4 + k, "start": t} for d in range(7) for k, t in ((0, "07:00 am"), (3, "09:30 pm"))]
    assert check_start("8:00 am", 4, week) == "08:00 am"  # day 1's morning, not day 0's night
    with pytest.raises(PortalError, match="before the period after"):
        check_start("10:00 pm", 4, week)
