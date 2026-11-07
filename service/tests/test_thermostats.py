import pytest

from mimir_climate.thermostats import ClimateError, check_setpoints, find_period, minutes, plan, system_names

LIMITS = {"heat": [60, 90], "cool": [60, 90], "deadband": 3, "unit": "F", "start_step": 15}


def schedule(days=1):
    """Two periods a day (morning 7:00, night 23:00), as a provider gives them: ids four apart per day."""
    periods = []
    for d in range(days):
        periods.append({"period_id": d * 4, "day": d, "slot": 0, "start": "07:00 am", "heat": 72, "cool": 80})
        periods.append({"period_id": d * 4 + 3, "day": d, "slot": 3, "start": "11:00 pm", "heat": 72, "cool": 84})
    return {"limits": LIMITS, "periods": periods}


def test_setpoints_inside_the_limits_with_the_deadband_pass():
    check_setpoints(72, 80, LIMITS)
    check_setpoints(77, 80, LIMITS)


@pytest.mark.parametrize(
    "heat,cool,why", [(59, 80, "heat 59 is outside"), (72, 91, "cool 91 is outside"), (78, 80, "at least 3 above")]
)
def test_setpoints_the_thermostat_would_refuse(heat, cool, why):
    with pytest.raises(ClimateError, match=why):
        check_setpoints(heat, cool, LIMITS)


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
def test_times_people_and_providers_write(text, mins):
    assert minutes(text) == mins


@pytest.mark.parametrize("text", ["25:00", "13:00 pm", "9:75", "noon", ""])
def test_times_that_are_not_times(text):
    with pytest.raises(ClimateError):
        minutes(text)


def test_a_setpoint_change_keeps_the_other_setpoint_and_the_start():
    [item] = plan(schedule(), 3, None, 75, None, False)
    assert (item["start"], item["heat"], item["cool"]) == (1380, 72, 75)
    assert (item["start_changed"], item["set_changed"]) == (False, True)


def test_a_start_change_alone():
    [item] = plan(schedule(), 3, None, None, "21:30", False)
    assert (item["start"], item["start_changed"], item["set_changed"]) == (1290, True, False)


def test_asking_for_what_is_already_set_changes_nothing():
    [item] = plan(schedule(), 0, 72, 80, "7:00 am", False)
    assert not item["start_changed"] and not item["set_changed"]


@pytest.mark.parametrize(
    "pid,start,why",
    [
        (3, "9:40 pm", "steps of 15"),
        (3, "6:30 am", "after the period before"),
        (0, "11:30 pm", "before the period after"),
    ],
)
def test_a_start_out_of_order_or_off_the_step_is_refused(pid, start, why):
    with pytest.raises(ClimateError, match=why):
        plan(schedule(), pid, None, None, start, False)


def test_every_day_touches_the_same_slot_on_each_day_and_checks_each_day_alone():
    items = plan(schedule(days=7), 0, None, None, "8:00 am", True)
    assert [i["period"]["period_id"] for i in items] == [0, 4, 8, 12, 16, 20, 24]
    assert all(i["start"] == 480 for i in items)
    with pytest.raises(ClimateError, match="before the period after"):  # past day 0's night, not day 1's
        plan(schedule(days=7), 4, None, None, "11:15 pm", False)


def test_one_period_that_does_not_fit_stops_the_whole_change():
    week = schedule(days=2)
    week["periods"][3]["start"] = "08:30 am"  # day 1's night starts early
    with pytest.raises(ClimateError, match="before the period after"):
        plan(week, 0, None, None, "8:30 am", True)


def test_an_unknown_period_names_the_real_ones():
    with pytest.raises(ClimateError, match="the periods are 0, 3"):
        find_period(schedule(), 1)


def test_systems_without_a_name_get_a_number():
    assert system_names(3, ["upstairs", "downstairs"]) == ["upstairs", "downstairs", "system 3"]
    assert system_names(1, ["upstairs", "downstairs"]) == ["upstairs"]
