"""Time source. Everything asks the clock for "now", so tests and the simulation can control time.

Times are naive datetimes in the clinic's local time zone: a slot at 09:00 means
09:00 at the clinic, whatever the server's time zone is.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class Clock:
    """The clinic's wall clock. Servers often run on UTC (Render does), so "now" is read in the
    clinic's own time zone, not the server's; otherwise "today" would flip hours early."""

    def __init__(self, timezone: str | None = None):
        self.zone = ZoneInfo(timezone) if timezone else None

    def now(self) -> datetime:
        return datetime.now(self.zone).replace(tzinfo=None, microsecond=0)


class FakeClock(Clock):
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when
