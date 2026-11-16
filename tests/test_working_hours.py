"""The clock Sellbird works to.

A mistyped time zone used to fail the whole configuration, which stopped the
heartbeat starting at all — and on a hosted install there was no terminal to
fix it from. These tests pin the three things that must hold: an obvious
spelling is understood, an impossible one falls back loudly instead of
fatally, and a clock a working day away from the prospects' is noticed.
"""

import pytest
import yaml

from sellbird.config import (FALLBACK_TIMEZONE, load_config, operator_timezone,
                             read_clock, resolve_timezone, timezone_mismatch)


def config_with(tmp_path, quiet=None, geography=None, send_window=None):
    data = yaml.safe_load(open("sellbird.yaml", encoding="utf-8"))
    if quiet:
        data.setdefault("usage", {}).setdefault("quiet_hours", {}).update(quiet)
    if geography is not None:
        data.setdefault("icp", {})["geography"] = geography
    if send_window is not None:
        data.setdefault("channels", {}).setdefault("email", {})["send_window"] = send_window
    path = tmp_path / "sellbird.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(str(path))


# ── Reading what someone wrote ───────────────────────────────────────


@pytest.mark.parametrize("written,expected", [
    ("India", "Asia/Kolkata"),
    ("IST", "Asia/Kolkata"),
    ("uk", "Europe/London"),
    ("Pacific", "America/Los_Angeles"),
    ("america/new_york", "America/New_York"),   # case alone is not a mistake
    ("Asia/Kolkata", "Asia/Kolkata"),
])
def test_a_plain_name_is_understood(written, expected):
    zone, problem = resolve_timezone(written)
    assert (zone, problem) == (expected, "")


def test_an_impossible_zone_falls_back_and_says_so():
    zone, problem = resolve_timezone("Nowhere/Nothing")
    assert zone == FALLBACK_TIMEZONE
    assert "Nowhere/Nothing" in problem and "Settings" in problem


def test_a_bad_timezone_no_longer_stops_sellbird_loading(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "Nowhere/Nothing"})
    assert config.usage.quiet_hours.zone == FALLBACK_TIMEZONE
    assert config.usage.quiet_hours.problem
    # The written value survives, so Settings can show the user their own typo.
    assert config.usage.quiet_hours.timezone == "Nowhere/Nothing"


def test_a_plain_name_loads_and_resolves(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "India"})
    assert config.usage.quiet_hours.zone == "Asia/Kolkata"
    assert config.usage.quiet_hours.problem == ""
    assert operator_timezone(config) == "Asia/Kolkata"


def test_the_clock_still_runs_with_a_broken_zone(tmp_path):
    from sellbird.main import in_quiet_hours, seconds_until_quiet_hours_end

    config = config_with(tmp_path, quiet={"timezone": "Nowhere/Nothing"})
    assert isinstance(in_quiet_hours(config), bool)
    assert seconds_until_quiet_hours_end(config) >= 0


# ── Reading the hours themselves ─────────────────────────────────────


@pytest.mark.parametrize("written,expected", [
    ("22:00", "22:00"),
    ("07:00Z", "07:00"),            # an offset here is noise: the zone is separate
    ("22:00+05:30", "22:00"),
    ("07:00:00", "07:00"),
    (1320, "22:00"),                # YAML 1.1 reads an unquoted 22:00 as base 60
    (420, "07:00"),
])
def test_every_shape_a_time_arrives_in_is_read(written, expected):
    assert read_clock(written, "22:00")[0] == expected


def test_a_time_nobody_can_read_falls_back_and_is_reported():
    text, problem = read_clock("9am", "22:00")
    assert text == "22:00"
    assert "9am" in problem


@pytest.mark.parametrize("quiet", [
    {"start": "22:00", "end": "07:00Z"},        # the one that took the agent down
    {"start": "22:00+05:30", "end": "07:00"},
    {"start": 1320, "end": 420},
    {"start": "9am", "end": "07:00"},
])
def test_a_broken_clock_never_stops_a_cycle(tmp_path, quiet):
    """An offset on one side and not the other made comparing them raise
    TypeError, which failed the heartbeat every 60 seconds forever."""
    from sellbird.main import in_quiet_hours, seconds_until_quiet_hours_end

    config = config_with(tmp_path, quiet=quiet)
    assert isinstance(in_quiet_hours(config), bool)
    assert seconds_until_quiet_hours_end(config) >= 0
    assert config.usage.quiet_hours.start == "22:00"
    assert config.usage.quiet_hours.end == "07:00"


def test_a_send_window_time_cannot_be_aware_either():
    from sellbird.config import SendWindowConfig

    window = SendWindowConfig(start="08:00+05:30", end="17:00Z")
    assert (window.start, window.end) == ("08:00", "17:00")


# ── Noticing a clock on the wrong continent ──────────────────────────


def test_an_operator_a_working_day_from_the_prospects_is_warned(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "Asia/Kolkata"},
                         geography=["United States", "Canada"])
    warning = timezone_mismatch(config)
    assert "rests through" in warning
    assert "Asia/Kolkata" in warning


def test_a_send_window_settles_it(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "Asia/Kolkata"},
                         geography=["United States"],
                         send_window={"start": "08:00", "end": "17:00"})
    assert timezone_mismatch(config) == ""


def test_the_same_continent_is_no_warning(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "America/New_York"},
                         geography=["United States", "Canada"])
    assert timezone_mismatch(config) == ""


def test_prospects_nowhere_in_particular_are_no_warning(tmp_path):
    config = config_with(tmp_path, quiet={"timezone": "Asia/Kolkata"},
                         geography=["Everywhere", "Remote"])
    assert timezone_mismatch(config) == ""
