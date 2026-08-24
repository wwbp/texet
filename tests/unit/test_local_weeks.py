"""Week boundaries in the participant's timezone.

Weeks used to be UTC for everyone. For a participant at UTC-5 that put the
boundary at Saturday 19:00 local, so the last five hours of their week fell
into the next one — and the summary for the week ran before those hours had
even happened. These helpers move the boundary onto their clock.
"""

import datetime

from app.response.utils import week_bounds_utc, week_start_for

_EST = datetime.timedelta(hours=-5)
_TOKYO = datetime.timedelta(hours=9)


def test_no_offset_falls_back_to_utc():
    moment = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=datetime.UTC)  # a Wednesday
    assert week_start_for(moment, None) == datetime.date(2026, 8, 16)


def test_saturday_evening_in_est_is_still_last_week():
    """23:00 UTC Saturday is 18:00 EST Saturday — the same week, not the next.
    In UTC terms nothing has rolled over yet either, so both agree here."""
    moment = datetime.datetime(2026, 8, 22, 23, 0, tzinfo=datetime.UTC)
    assert week_start_for(moment, _EST) == datetime.date(2026, 8, 16)


def test_sunday_before_dawn_utc_is_still_last_week_in_est():
    """02:00 UTC Sunday is 21:00 EST Saturday. UTC has rolled the week over;
    the participant has not. This is the window the old code got wrong."""
    moment = datetime.datetime(2026, 8, 23, 2, 0, tzinfo=datetime.UTC)
    assert week_start_for(moment, None) == datetime.date(2026, 8, 23)
    assert week_start_for(moment, _EST) == datetime.date(2026, 8, 16)


def test_sunday_morning_in_tokyo_rolls_over_before_utc():
    """The mirror case: 16:00 UTC Saturday is 01:00 Sunday in Tokyo."""
    moment = datetime.datetime(2026, 8, 22, 16, 0, tzinfo=datetime.UTC)
    assert week_start_for(moment, None) == datetime.date(2026, 8, 16)
    assert week_start_for(moment, _TOKYO) == datetime.date(2026, 8, 23)


def test_bounds_without_offset_are_utc_midnight():
    start, end = week_bounds_utc(datetime.date(2026, 8, 16), None)
    assert start == datetime.datetime(2026, 8, 16, 0, 0, tzinfo=datetime.UTC)
    assert end == datetime.datetime(2026, 8, 23, 0, 0, tzinfo=datetime.UTC)


def test_bounds_shift_by_the_offset():
    """Local midnight in EST is 05:00 UTC, so the whole window slides."""
    start, end = week_bounds_utc(datetime.date(2026, 8, 16), _EST)
    assert start == datetime.datetime(2026, 8, 16, 5, 0, tzinfo=datetime.UTC)
    assert end == datetime.datetime(2026, 8, 23, 5, 0, tzinfo=datetime.UTC)


def test_bounds_are_always_exactly_seven_days():
    for offset in (None, _EST, _TOKYO, datetime.timedelta(hours=5, minutes=30)):
        start, end = week_bounds_utc(datetime.date(2026, 8, 16), offset)
        assert end - start == datetime.timedelta(days=7)


def test_consecutive_weeks_meet_without_gap_or_overlap():
    """A message must land in exactly one week, whatever the offset."""
    _, first_end = week_bounds_utc(datetime.date(2026, 8, 16), _EST)
    second_start, _ = week_bounds_utc(datetime.date(2026, 8, 23), _EST)
    assert first_end == second_start


def test_half_hour_offsets_are_handled():
    india = datetime.timedelta(hours=5, minutes=30)
    start, _ = week_bounds_utc(datetime.date(2026, 8, 16), india)
    assert start == datetime.datetime(2026, 8, 15, 18, 30, tzinfo=datetime.UTC)
