"""Time source. Everything asks the clock for "now", so tests and the simulation can control time.

Times are naive datetimes in the clinic's local time zone: a slot at 09:00 means
09:00 at the clinic, whatever the server's time zone is.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class Clock:
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)


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
