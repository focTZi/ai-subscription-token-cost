"""Calendar-day periods in the system time zone, and log timestamp parsing."""
from datetime import date, datetime, timedelta, timezone
import re

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH = re.compile(r"(\d{4})-(\d{2})")
_DAYS = re.compile(r"([1-9]\d{0,4})d")
# Seconds and an explicit zone are required; fractions are ignored on purpose:
# they never change the calendar day and must not round across midnight.
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))")


def parse_date(value):
    try:
        if not _DATE.fullmatch(value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid date {value!r}, expected YYYY-MM-DD") from None


def parse_timestamp(value):
    """Return an aware UTC datetime, or None for anything that is not a valid timestamp."""
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.fullmatch(value)
    if not match:
        return None
    day, hour, minute, second, sign, offset_hours, offset_minutes = match.groups()
    if int(hour) > 23:  # Newer Pythons accept 24:00:00 as the next midnight.
        return None
    try:
        moment = datetime.fromisoformat(f"{day}T{hour}:{minute}:{second}").replace(tzinfo=timezone.utc)
        if sign:
            if int(offset_hours) > 23 or int(offset_minutes) > 59:
                return None
            offset = timedelta(hours=int(offset_hours), minutes=int(offset_minutes))
            moment = moment - offset if sign == "+" else moment + offset
        return moment
    except (ValueError, OverflowError):
        return None


GROUPS = ("model", "day", "week", "month")


def local_day(moment):
    """Calendar day of an aware datetime in the system time zone (honors $TZ)."""
    return moment.astimezone().date()


def local_today():
    return datetime.now().astimezone().date()


def timezone_name():
    return datetime.now().astimezone().tzname() or "local time"


def bucket(day, group):
    """Label of the day/ISO week/month a calendar day belongs to."""
    if day is None:
        return "undated"
    if group == "week":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if group == "month":
        return day.strftime("%Y-%m")
    return day.isoformat()


class Period:
    """Inclusive range of local calendar days; either bound may be open."""

    def __init__(self, start=None, end=None, label=None):
        if start and end and start > end:
            raise ValueError("the start date must not be after the end date")
        self.start, self.end = start, end
        self.label = label or self._default_label()

    def _default_label(self):
        return "custom range" if self.active else "all time"

    @property
    def active(self):
        return self.start is not None or self.end is not None

    def contains(self, day):
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)

    def to_json(self):
        return dict(label=self.label, from_date=self.start.isoformat() if self.start else None,
                    to_date=self.end.isoformat() if self.end else None, timezone=timezone_name())


def last_days(days, today):
    return Period(today - timedelta(days=days - 1), today, f"last {days} days")


def month(year, number, today):
    start = date(year, number, 1)
    following = date(year + number // 12, number % 12 + 1, 1)
    end = min(following - timedelta(days=1), today) if start <= today else following - timedelta(days=1)
    return Period(start, end, start.strftime("%B %Y"))


def this_month(today):
    return Period(today.replace(day=1), today, "this month")


def last_month(today):
    end = today.replace(day=1) - timedelta(days=1)
    return Period(end.replace(day=1), end, "last month")


def parse_period(value, today):
    """Parse --period: Nd, this-month, last-month, YYYY-MM or all."""
    value = value.strip().lower()
    if value == "all":
        return Period()
    if value == "this-month":
        return this_month(today)
    if value == "last-month":
        return last_month(today)
    match = _DAYS.fullmatch(value)
    if match:
        return last_days(int(match.group(1)), today)
    match = _MONTH.fullmatch(value)
    if match and 1 <= int(match.group(2)) <= 12:
        return month(int(match.group(1)), int(match.group(2)), today)
    raise ValueError(f"invalid period {value!r}; use e.g. 30d, 7d, this-month, last-month, 2026-10 or all")
