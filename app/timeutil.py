from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

BUDAPEST = ZoneInfo("Europe/Budapest")


def month_of(ts: datetime) -> date:
    local = ts.astimezone(BUDAPEST)
    return date(local.year, local.month, 1)


def _add_months(month: date, n: int) -> date:
    index = month.year * 12 + (month.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def previous_month(today: date | None = None) -> date:
    if today is None:
        today = datetime.now(BUDAPEST).date()
    return _add_months(today.replace(day=1), -1)


def months_between(start: date, end: date) -> list[date]:
    months = []
    current = start.replace(day=1)
    last = end.replace(day=1)
    while current <= last:
        months.append(current)
        current = _add_months(current, 1)
    return months


def month_bounds_utc(month: date) -> tuple[datetime, datetime]:
    first = month.replace(day=1)
    start = datetime.combine(first, time.min, tzinfo=BUDAPEST)
    end = datetime.combine(_add_months(first, 1), time.min, tzinfo=BUDAPEST)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
