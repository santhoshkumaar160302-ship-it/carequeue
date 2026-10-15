"""Doctors' working hours and time off, and what happens to patients when they change.

Slots are generated on a fixed grid (8:00-18:00 by default). Whether a slot can be booked
is decided here: a slot outside the doctor's weekly hours, or inside a block of time off,
is marked unavailable (`Slot.blocked_reason`).

When a doctor blocks time that already has patients in it, those visits are cancelled,
the patients are told, and each one goes to the front of the waitlist for that doctor, so
they're offered the next suitable opening first. Opening time up again does the reverse:
each newly bookable slot is offered to the waitlist like any other freed slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, select

from .clinic import Clinic, ClinicError, Conflict, NotFound, fmt_when
from .models import Appointment, Provider, Slot, TimeOff, WaitlistEntry, WorkingHours

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
REBOOK_WINDOW_DAYS = 14  # how far ahead a displaced patient is offered a new slot


@dataclass
class Day:
    weekday: int
    start_minute: int
    end_minute: int


def _minute(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


class Availability:
    def __init__(self, clinic: Clinic):
        self.clinic = clinic
        self.Session = clinic.Session
        self.clock = clinic.clock

    # ------------------------------------------------------------------ reads
    def hours(self, provider_id: int) -> list[dict]:
        with self.Session() as s:
            q = select(WorkingHours).where(WorkingHours.provider_id == provider_id)
            rows = {h.weekday: h for h in s.scalars(q)}
        return [{"weekday": d, "name": WEEKDAYS[d], "working": d in rows,
                 "start": _hhmm(rows[d].start_minute) if d in rows else None,
                 "end": _hhmm(rows[d].end_minute) if d in rows else None} for d in range(7)]

    def time_off(self, provider_id: int) -> list[dict]:
        now = self.clock.now()
        with self.Session() as s:
            rows = s.scalars(select(TimeOff).where(TimeOff.provider_id == provider_id, TimeOff.end > now)
                             .order_by(TimeOff.start)).all()
            return [{"id": t.id, "start": t.start.isoformat(), "end": t.end.isoformat(), "reason": t.reason,
                     "label": _range_label(t.start, t.end)} for t in rows]

    # ------------------------------------------------------------------ the rule
    @staticmethod
    def _blocked_reason(slot: Slot, hours: dict[int, Day] | None, offs: list[tuple[datetime, datetime]]) -> str | None:
        if hours is not None:
            day = hours.get(slot.start.weekday())
            if day is None or _minute(slot.start) < day.start_minute or _minute(slot.end) > day.end_minute:
                return "hours"
        if any(start < slot.end and end > slot.start for start, end in offs):
            return "time_off"
        return None

    def _plan(self, s, provider_id: int, hours: list[Day] | None, offs: list[tuple[datetime, datetime]]):
        """Which future slots change state, without changing anything."""
        now = self.clock.now()
        by_day = None if hours is None else {d.weekday: d for d in hours}
        changes = []
        for slot in s.scalars(select(Slot).where(Slot.provider_id == provider_id, Slot.start > now)
                              .order_by(Slot.start)):
            want = self._blocked_reason(slot, by_day, offs)
            if want != slot.blocked_reason:
                changes.append((slot, want))
        return changes

    def _current(self, s, provider_id: int):
        rows = s.scalars(select(WorkingHours).where(WorkingHours.provider_id == provider_id)).all()
        hours = [Day(h.weekday, h.start_minute, h.end_minute) for h in rows] or None
        offs = [(t.start, t.end) for t in s.scalars(select(TimeOff).where(TimeOff.provider_id == provider_id))]
        return hours, offs

    def _affected(self, s, changes) -> list[dict]:
        newly_blocked = [slot.id for slot, want in changes if want and not slot.blocked_reason]
        if not newly_blocked:
            return []
        rows = s.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id)
                         .where(Appointment.slot_id.in_(newly_blocked), Appointment.status == "booked")
                         .order_by(Slot.start)).all()
        return [{"appointment_id": a.id, "patient_id": a.patient_id, "patient": a.patient.name,
                 "start": sl.start.isoformat(), "label": fmt_when(sl.start)} for a, sl in rows]

    # ------------------------------------------------------------------ previews
    def preview_hours(self, provider_id: int, days: list[dict]) -> dict:
        hours = _parse_days(days)
        with self.Session() as s:
            _, offs = self._current(s, provider_id)
            changes = self._plan(s, provider_id, hours, offs)
            return self._summary(s, changes)

    def preview_time_off(self, provider_id: int, start: datetime, end: datetime) -> dict:
        _check_range(start, end)
        with self.Session() as s:
            hours, offs = self._current(s, provider_id)
            changes = self._plan(s, provider_id, hours, [*offs, (start, end)])
            return self._summary(s, changes)

    def _summary(self, s, changes) -> dict:
        return {"blocked": sum(1 for slot, want in changes if want and not slot.blocked_reason),
                "opened": sum(1 for _, want in changes if not want),
                "affected": self._affected(s, changes)}

    # ------------------------------------------------------------------ writes
    def set_hours(self, provider_id: int, days: list[dict]) -> dict:
        hours = _parse_days(days)
        with self.Session() as s:
            if s.get(Provider, provider_id) is None:
                raise NotFound(f"provider {provider_id} not found")
            s.execute(delete(WorkingHours).where(WorkingHours.provider_id == provider_id))
            s.add_all(WorkingHours(provider_id=provider_id, weekday=d.weekday, start_minute=d.start_minute,
                                   end_minute=d.end_minute) for d in hours)
            s.commit()
        return {"hours": self.hours(provider_id), **self.reconcile(provider_id)}

    def add_time_off(self, provider_id: int, start: datetime, end: datetime, reason: str = "") -> dict:
        _check_range(start, end)
        if end <= self.clock.now():
            raise ClinicError("That time has already passed.")
        with self.Session() as s:
            off = TimeOff(provider_id=provider_id, start=start, end=end, reason=reason.strip()[:100],
                          created_at=self.clock.now())
            s.add(off)
            s.commit()
            off_id = off.id
        return {"id": off_id, **self.reconcile(provider_id)}

    def remove_time_off(self, provider_id: int, time_off_id: int) -> dict:
        with self.Session() as s:
            off = s.get(TimeOff, time_off_id)
            if off is None or off.provider_id != provider_id:
                raise NotFound(f"time off {time_off_id} not found")
            s.delete(off)
            s.commit()
        return self.reconcile(provider_id)

    def reconcile(self, provider_id: int) -> dict:
        """Bring every future slot in line with the doctor's hours and time off."""
        with self.Session() as s:
            hours, offs = self._current(s, provider_id)
            changes = self._plan(s, provider_id, hours, offs)
            affected = self._affected(s, changes)
            opened = []
            for slot, want in changes:
                if not want and slot.blocked_reason:
                    opened.append(slot.id)
                slot.blocked_reason = want
            for slot_id in opened:
                self.clinic._emit(s, "slot_freed", {"slot_id": slot_id, "cause": "reopened"})
            # Pending offers in newly blocked slots are withdrawn; the patient goes back in line.
            blocked_ids = [slot.id for slot, want in changes if want]
            withdrawn = s.scalars(select(Appointment).where(Appointment.slot_id.in_(blocked_ids),
                                                            Appointment.status == "held")).all() if blocked_ids else []
            for hold in withdrawn:
                hold.status, hold.updated_at, hold.cancel_reason = "cancelled", self.clock.now(), "doctor"
                if hold.waitlist_entry_id:
                    entry = s.get(WaitlistEntry, hold.waitlist_entry_id)
                    if entry.status == "offered":
                        entry.status = "waiting"
                self.clinic._notify(s, hold.patient_id, "offer_withdrawn",
                                    f"The {fmt_when(hold.slot.start)} slot you were offered is no longer "
                                    "available. You're still on the waitlist.")
            s.commit()
            days = {slot.start.date() for slot, _ in changes}
        for day in days:
            self.clinic.cache.invalidate(provider_id, day)
        rebooking = [{"patient": a["patient"], "was": a["label"],
                      **self.displace(a["appointment_id"], "doctor_unavailable")} for a in affected]
        return {"blocked": sum(1 for _, want in changes if want), "opened": len(opened), "rebooking": rebooking}

    def displace(self, appointment_id: int, reason: str = "doctor") -> dict:
        """The doctor can't see this patient: cancel the visit, tell them, and put them first in line."""
        now = self.clock.now()
        with self.Session() as s:
            appt = s.get(Appointment, appointment_id)
            if appt is None:
                raise NotFound(f"appointment {appointment_id} not found")
            if appt.status != "booked":
                raise Conflict(f"Appointment {appointment_id} is {appt.status}, not booked.")
            slot = appt.slot
            appt.status, appt.updated_at, appt.cancel_reason = "cancelled", now, reason
            # Any request to move this visit earlier no longer makes sense.
            for entry in s.scalars(select(WaitlistEntry).where(WaitlistEntry.replaces_appointment_id == appt.id,
                                                               WaitlistEntry.status == "waiting")):
                entry.status = "cancelled"
            entry = WaitlistEntry(patient_id=appt.patient_id, provider_id=slot.provider_id, specialty=None,
                                  earliest=now.date(), latest=now.date() + timedelta(days=REBOOK_WINDOW_DAYS),
                                  time_of_day="any", status="waiting", created_at=now, priority=1)
            s.add(entry)
            s.flush()
            self.clinic._emit(s, "slot_freed", {"slot_id": slot.id, "cause": "cancelled",
                                                "appointment_id": appt.id, "reason": reason})
            self.clinic._emit(s, "waitlist_joined", {"entry_id": entry.id, "patient_id": appt.patient_id,
                                                     "cause": "displaced"})
            self.clinic._notify(s, appt.patient_id, "cancellation",
                                f"{slot.provider.name} can't see you on {fmt_when(slot.start)}, sorry. "
                                "You're first in line for the next opening and we'll offer you a new time.")
            s.commit()
            entry_id, provider_id, day = entry.id, slot.provider_id, slot.start.date()
        self.clinic.cache.invalidate(provider_id, day)
        offered = self.clinic._offer_open_slot_to(entry_id)
        return {"entry_id": entry_id, "offered_appointment_id": offered}


def _parse_days(days: list[dict]) -> list[Day]:
    out, seen = [], set()
    for d in days:
        if not d.get("working", True):
            continue
        weekday = int(d["weekday"])
        if not 0 <= weekday <= 6 or weekday in seen:
            raise ClinicError("Each weekday (0 = Monday ... 6 = Sunday) can appear once.")
        seen.add(weekday)
        start, end = _parse_hhmm(d["start"]), _parse_hhmm(d["end"])
        if end <= start:
            raise ClinicError(f"On {WEEKDAYS[weekday]} the end time must be after the start time.")
        out.append(Day(weekday, start, end))
    if not out:
        raise ClinicError("Keep at least one working day. For a break, add time off instead.")
    return out


def _parse_hhmm(value: str) -> int:
    try:
        t = time.fromisoformat(value)
    except (TypeError, ValueError):
        raise ClinicError(f"Times look like 09:00, not {value!r}.") from None
    return t.hour * 60 + t.minute


def _check_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ClinicError("Time off must end after it starts.")
    if end - start > timedelta(days=60):
        raise ClinicError("Time off can be at most 60 days at a time.")


def _range_label(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%a %b} {start.day}, {start:%H:%M}-{end:%H:%M}"
    if start.time() == time.min and end.time() == time.min:
        last: date = (end - timedelta(days=1)).date()
        return f"{start:%a %b} {start.day}" + ("" if last == start.date() else f" - {last:%a %b} {last.day}")
    return f"{start:%a %b} {start.day} {start:%H:%M} - {end:%a %b} {end.day} {end:%H:%M}"
