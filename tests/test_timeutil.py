"""IST → UTC schedule parsing (gelio/timeutil.py)."""

from __future__ import annotations

import pytest

from gelio.timeutil import (
    IST,
    ScheduleParseError,
    ist_to_utc,
    parse_schedule_input,
    utc_to_ist,
)


def test_ist_to_utc_subtracts_5_30():
    dt = ist_to_utc("2026-06-13 18:00")
    assert dt.isoformat() == "2026-06-13T12:30:00+00:00"


def test_ist_to_utc_alternate_formats():
    assert ist_to_utc("13-06-2026 18:00").isoformat() == "2026-06-13T12:30:00+00:00"
    assert ist_to_utc("2026-06-13 18:00:30").isoformat() == "2026-06-13T12:30:30+00:00"


def test_ist_midnight_crosses_date():
    # 02:00 IST is the previous day 20:30 UTC.
    assert ist_to_utc("2026-06-13 02:00").isoformat() == "2026-06-12T20:30:00+00:00"


def test_parse_schedule_input_naive_is_ist():
    assert parse_schedule_input("2026-06-13 18:00") == "2026-06-13T12:30:00+00:00"
    assert parse_schedule_input("2026-06-13T18:00:00") == "2026-06-13T12:30:00+00:00"


def test_parse_schedule_input_explicit_utc_respected():
    assert parse_schedule_input("2026-06-13T12:30:00Z") == "2026-06-13T12:30:00+00:00"
    assert parse_schedule_input("2026-06-13T18:00:00+05:30") == "2026-06-13T12:30:00+00:00"


def test_parse_schedule_input_rejects_garbage():
    with pytest.raises(ScheduleParseError):
        parse_schedule_input("next tuesday at noon")


def test_utc_to_ist_roundtrip():
    ist = utc_to_ist("2026-06-13T12:30:00+00:00")
    assert ist.tzinfo == IST
    assert ist.strftime("%Y-%m-%d %H:%M") == "2026-06-13 18:00"
