"""Visits from both sides: a doctor's day and patient list, and a patient's history.

Every query here is scoped by the caller (a provider id or a patient id), and the API
passes the signed-in user's own id, so nobody can read another person's records.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from .clinic import Clinic, ClinicError, Conflict, NotFound, fmt_when
from .models import ACTIVE_STATUSES, SHOWN_STATUSES, Appointment, Notification, Patient, Provider, Slot, WaitlistEntry

DONE_STATUSES = ("completed", "no_show")


def _visit(a: Appointment, sl: Slot, provider: Provider | None = None, patient: Patient | None = None) -> dict:
    out = {"id": a.id, "status": a.status, "source": a.source, "slot_id": sl.id, "start": sl.start.isoformat(),
           "end": sl.end.isoformat(), "label": fmt_when(sl.start), "note": a.note, "cancel_reason": a.cancel_reason,
           "hold_expires_at": a.hold_expires_at.isoformat() if a.hold_expires_at else None}
    if provider is not None:
        out |= {"provider_id": provider.id, "provider": provider.name, "specialty": provider.specialty}
    if patient is not None:
        out |= {"patient_id": patient.id, "patient": patient.name, "phone": patient.phone}
    return out


class Visits:
    def __init__(self, clinic: Clinic):
        self.clinic = clinic
        self.Session = clinic.Session
        self.clock = clinic.clock

    # ------------------------------------------------------------------ doctor
    def today(self, provider_id: int) -> dict:
        """The doctor's day: every visit today, who's next, and anything still to be written up."""
        now = self.clock.now()
        day = now.date()
        with self.Session() as s:
            provider = s.get(Provider, provider_id)
            rows = s.execute(
                select(Appointment, Slot, Patient).join(Slot, Appointment.slot_id == Slot.id)
                .join(Patient, Appointment.patient_id == Patient.id)
                .where(Slot.provider_id == provider_id, Appointment.status.in_(SHOWN_STATUSES),
                       Slot.start >= datetime.combine(day, time.min),
                       Slot.start < datetime.combine(day + timedelta(days=1), time.min))
                .order_by(Slot.start)).all()
            visits = [_visit(a, sl, patient=p) | {"past": sl.start <= now, "now": sl.start <= now < sl.end}
                      for a, sl, p in rows]
            upcoming = s.execute(
                select(Appointment, Slot, Patient).join(Slot, Appointment.slot_id == Slot.id)
                .join(Patient, Appointment.patient_id == Patient.id)
                .where(Slot.provider_id == provider_id, Appointment.status == "booked", Slot.end > now)
                .order_by(Slot.start).limit(1)).first()
            next_patient = None
            if upcoming:
                a, sl, p = upcoming
                last = self._last_visit(s, provider_id, p.id, before=sl.start)
                next_patient = _visit(a, sl, patient=p) | {"now": sl.start <= now, "last_visit": last}
            # Earlier visits that already started but haven't been marked done yet.
            to_close = s.scalar(select(func.count()).select_from(Appointment).join(Slot)
                                .where(Slot.provider_id == provider_id, Appointment.status == "booked",
                                       Slot.start <= now)) or 0
            open_today = s.scalar(select(func.count()).select_from(Slot).where(
                Slot.provider_id == provider_id, Slot.blocked_reason.is_(None), Slot.start > now,
                Slot.start < datetime.combine(day + timedelta(days=1), time.min),
                ~Slot.id.in_(select(Appointment.slot_id).where(Appointment.status.in_(ACTIVE_STATUSES))))) or 0
            waiting = s.scalar(select(func.count()).select_from(WaitlistEntry).where(
                WaitlistEntry.provider_id == provider_id, WaitlistEntry.status.in_(("waiting", "offered")))) or 0
        return {"provider": {"id": provider.id, "name": provider.name, "specialty": provider.specialty},
                "date": day.isoformat(), "visits": visits, "next": next_patient,
                "counts": {"booked": sum(1 for v in visits if v["status"] in ("booked", "completed", "no_show")),
                           "done": sum(1 for v in visits if v["status"] in DONE_STATUSES),
                           "to_close": to_close, "open_today": open_today, "waitlist": waiting}}

    def _last_visit(self, s, provider_id: int, patient_id: int, before: datetime) -> dict | None:
        row = s.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id)
                        .where(Slot.provider_id == provider_id, Appointment.patient_id == patient_id,
                               Appointment.status == "completed", Slot.start < before)
                        .order_by(Slot.start.desc()).limit(1)).first()
        return _visit(*row) if row else None

    def _own(self, s, provider_id: int, appointment_id: int) -> Appointment:
        appt = s.get(Appointment, appointment_id)
        if appt is None or appt.slot.provider_id != provider_id:
            raise NotFound(f"appointment {appointment_id} not found")
        return appt

    def own_check(self, provider_id: int, appointment_id: int) -> None:
        with self.Session() as s:
            self._own(s, provider_id, appointment_id)

    def close(self, provider_id: int, appointment_id: int, outcome: str, note: str | None = None) -> dict:
        """Mark a visit as seen (completed) or as a no-show, once its start time has passed."""
        if outcome not in DONE_STATUSES:
            raise Conflict("Outcome must be completed or no_show.")
        now = self.clock.now()
        with self.Session() as s:
            appt = self._own(s, provider_id, appointment_id)
            if appt.status not in ("booked", *DONE_STATUSES):
                raise Conflict(f"This visit is {appt.status}.")
            if appt.slot.start > now:
                raise Conflict("A visit can be closed once it has started.")
            appt.status, appt.updated_at = outcome, now
            if note is not None:
                appt.note = note.strip()[:2000] or None
            self.clinic._emit(s, "visit_" + outcome, {"appointment_id": appt.id, "slot_id": appt.slot_id,
                                                      "patient_id": appt.patient_id})
            s.commit()
            self.clinic._invalidate(appt.slot)
            return _visit(appt, appt.slot)

    def set_note(self, provider_id: int, appointment_id: int, note: str) -> dict:
        with self.Session() as s:
            appt = self._own(s, provider_id, appointment_id)
            if appt.status not in DONE_STATUSES:
                raise Conflict("Notes are added to visits that have happened.")
            appt.note, appt.updated_at = note.strip()[:2000] or None, self.clock.now()
            s.commit()
            return _visit(appt, appt.slot)

    def doctor_patients(self, provider_id: int, query: str = "", group: str = "all") -> list[dict]:
        """Everyone this doctor has seen or may see, with their last and next visit.

        group="mine": patients this doctor has already seen (at least one completed visit).
        group="new":  patients who haven't been seen by this doctor yet, but are booked
                      with them or waiting for them on the waitlist.
        """
        if group not in ("all", "mine", "new"):
            raise ClinicError("group must be all, mine or new")
        now = self.clock.now()
        needle = query.strip().lower()
        people: dict[int, dict] = {}

        def row_for(p: Patient) -> dict:
            return people.setdefault(p.id, {"id": p.id, "name": p.name, "phone": p.phone, "visits": 0,
                                            "last_visit": None, "next_visit": None, "waiting": False})
        with self.Session() as s:
            q = (select(Appointment, Slot, Patient).join(Slot, Appointment.slot_id == Slot.id)
                 .join(Patient, Appointment.patient_id == Patient.id)
                 .where(Slot.provider_id == provider_id,
                        Appointment.status.in_(("booked", "held", *DONE_STATUSES)))
                 .order_by(Slot.start))
            w = (select(WaitlistEntry, Patient).join(Patient, WaitlistEntry.patient_id == Patient.id)
                 .where(WaitlistEntry.provider_id == provider_id,
                        WaitlistEntry.status.in_(("waiting", "offered"))))
            if needle:
                q = q.where(func.lower(Patient.name).contains(needle))
                w = w.where(func.lower(Patient.name).contains(needle))
            for a, sl, p in s.execute(q):
                row = row_for(p)
                if a.status == "completed":
                    row["visits"] += 1
                    row["last_visit"] = {"start": sl.start.isoformat(), "label": fmt_when(sl.start)}
                elif a.status in ACTIVE_STATUSES and sl.start > now and row["next_visit"] is None:
                    row["next_visit"] = {"start": sl.start.isoformat(), "label": fmt_when(sl.start),
                                         "appointment_id": a.id, "offer": a.status == "held"}
            for _entry, p in s.execute(w):
                row_for(p)["waiting"] = True
        for row in people.values():
            row["group"] = "mine" if row["visits"] else "new"
        # A new patient with no booking and no wait is someone whose only visit was missed or not
        # yet written up; they still belong here, so the doctor can find them.
        rows = [r for r in people.values() if group == "all" or r["group"] == group]
        # Patients with something coming up first (soonest first), then everyone else by name.
        return sorted(rows, key=lambda r: (r["next_visit"] is None,
                                           r["next_visit"]["start"] if r["next_visit"] else "", r["name"]))

    def doctor_patient(self, provider_id: int, patient_id: int) -> dict:
        """One patient's visits with this doctor only. Other doctors' notes stay private."""
        with self.Session() as s:
            patient = s.get(Patient, patient_id)
            rows = s.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id)
                             .where(Slot.provider_id == provider_id, Appointment.patient_id == patient_id,
                                    Appointment.status.in_(("booked", "held", "cancelled", *DONE_STATUSES)))
                             .order_by(Slot.start.desc())).all()
            waits = s.scalars(select(WaitlistEntry).where(
                WaitlistEntry.provider_id == provider_id, WaitlistEntry.patient_id == patient_id,
                WaitlistEntry.status.in_(("waiting", "offered")))).all()
            if patient is None or not (rows or waits):
                raise NotFound(f"patient {patient_id} not found")
            visits = [_visit(a, sl) for a, sl in rows]
            waiting = [{"id": e.id, "status": e.status, "earliest": e.earliest.isoformat(),
                        "latest": e.latest.isoformat(), "time_of_day": e.time_of_day, "priority": e.priority}
                       for e in waits]
        now = self.clock.now()
        seen = sum(1 for v in visits if v["status"] == "completed")
        return {"id": patient.id, "name": patient.name, "phone": patient.phone,
                "group": "mine" if seen else "new", "visits": seen, "waiting": waiting,
                "upcoming": [v for v in reversed(visits) if v["status"] in ACTIVE_STATUSES
                             and datetime.fromisoformat(v["start"]) > now],
                "history": [v for v in visits if v["status"] in DONE_STATUSES
                            or (v["status"] == "cancelled" and datetime.fromisoformat(v["start"]) <= now)
                            or v["status"] == "booked" and datetime.fromisoformat(v["start"]) <= now]}

    # ------------------------------------------------------------------ patient
    def overview(self, patient_id: int) -> dict:
        """A patient's home page: the next visit, offers waiting for an answer, the rest of the plan."""
        now = self.clock.now()
        with self.Session() as s:
            patient = s.get(Patient, patient_id)
            rows = s.execute(select(Appointment, Slot, Provider).join(Slot, Appointment.slot_id == Slot.id)
                             .join(Provider, Slot.provider_id == Provider.id)
                             .where(Appointment.patient_id == patient_id, Appointment.status.in_(ACTIVE_STATUSES),
                                    Slot.end > now)
                             .order_by(Slot.start)).all()
            booked = [_visit(a, sl, p) for a, sl, p in rows if a.status == "booked"]
            offers = [_visit(a, sl, p) for a, sl, p in rows if a.status == "held"]
            last = s.execute(select(Appointment, Slot, Provider).join(Slot, Appointment.slot_id == Slot.id)
                             .join(Provider, Slot.provider_id == Provider.id)
                             .where(Appointment.patient_id == patient_id, Appointment.status == "completed")
                             .order_by(Slot.start.desc()).limit(1)).first()
            notes = s.scalars(select(Notification).where(Notification.patient_id == patient_id)
                              .order_by(Notification.id.desc()).limit(12)).all()
            waits = s.execute(select(WaitlistEntry, Provider)
                              .outerjoin(Provider, WaitlistEntry.provider_id == Provider.id)
                              .where(WaitlistEntry.patient_id == patient_id,
                                     WaitlistEntry.status.in_(("waiting", "offered")))
                              .order_by(WaitlistEntry.created_at)).all()
        positions = {w["id"]: w["position"] for w in self.clinic.waitlist()}
        return {
            "patient": {"id": patient.id, "name": patient.name, "phone": patient.phone},
            "next": booked[0] if booked else None,
            "upcoming": booked,
            "offers": offers,
            "last_visit": _visit(*last) if last else None,
            "waitlist": [{"id": e.id, "status": e.status, "provider": p.name if p else None,
                          "specialty": e.specialty or (p.specialty if p else None),
                          "earliest": e.earliest.isoformat(), "latest": e.latest.isoformat(),
                          "time_of_day": e.time_of_day, "priority": e.priority,
                          "replaces_appointment_id": e.replaces_appointment_id,
                          "position": positions.get(e.id)} for e, p in waits],
            "notifications": [{"id": n.id, "kind": n.kind, "message": n.message, "at": n.created_at.isoformat()}
                              for n in notes],
        }

    def history(self, patient_id: int) -> list[dict]:
        """Past visits (seen, missed) and cancellations, newest first, with the doctor's notes."""
        now = self.clock.now()
        with self.Session() as s:
            rows = s.execute(select(Appointment, Slot, Provider).join(Slot, Appointment.slot_id == Slot.id)
                             .join(Provider, Slot.provider_id == Provider.id)
                             .where(Appointment.patient_id == patient_id,
                                    Appointment.status.in_(("completed", "no_show", "cancelled", "booked")))
                             .order_by(Slot.start.desc()).limit(200)).all()
            return [_visit(a, sl, p) for a, sl, p in rows
                    if a.status in DONE_STATUSES or (a.status == "booked" and sl.start <= now)
                    or (a.status == "cancelled" and a.cancel_reason != "moved_up")]

    def owns(self, patient_id: int, appointment_id: int) -> None:
        with self.Session() as s:
            appt = s.get(Appointment, appointment_id)
            if appt is None or appt.patient_id != patient_id:
                raise NotFound(f"appointment {appointment_id} not found")

    def owns_entry(self, patient_id: int, entry_id: int) -> None:
        with self.Session() as s:
            entry = s.get(WaitlistEntry, entry_id)
            if entry is None or entry.patient_id != patient_id:
                raise NotFound(f"waitlist entry {entry_id} not found")

    def patient_week(self, patient_id: int, provider_id: int, start) -> dict:
        """A doctor's week as a patient may see it: open, taken, or theirs. No other names."""
        week = self.clinic.week(provider_id, start)
        for day in week["days"]:
            for sl in day["slots"]:
                mine = sl["patient_id"] == patient_id
                state = sl["state"]
                if mine:
                    sl["state"] = {"held": "offer", "booked": "mine"}.get(state, state)
                elif state == "blocked":
                    sl["state"] = "unavailable"
                elif state != "open":
                    sl["state"] = "taken"
                if not mine:
                    sl["patient_id"] = sl["appointment_id"] = None
                sl.pop("blocked_reason", None)
        return week

    def doctor_week(self, provider_id: int, start) -> dict:
        week = self.clinic.week(provider_id, start)
        ids = {sl["patient_id"] for d in week["days"] for sl in d["slots"] if sl["patient_id"]}
        with self.Session() as s:
            names = dict(s.execute(select(Patient.id, Patient.name).where(Patient.id.in_(ids))).all()) if ids else {}
        for day in week["days"]:
            for sl in day["slots"]:
                sl["patient"] = names.get(sl["patient_id"])
        return week
