from datetime import date, datetime, timezone

from app.timeutil import month_bounds_utc, month_of, months_between, previous_month


def test_month_of_budapest_boundary():
    ts = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)  # 00:30 okt. 1. CEST
    assert month_of(ts) == date(2026, 10, 1)


def test_previous_month_on_first_of_january():
    assert previous_month(date(2026, 1, 1)) == date(2025, 12, 1)


def test_months_between_inclusive():
    assert months_between(date(2025, 11, 1), date(2026, 2, 1)) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


def test_month_bounds_utc_cet_winter():
    start, end = month_bounds_utc(date(2026, 1, 1))
    assert start == datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 31, 23, 0, tzinfo=timezone.utc)
