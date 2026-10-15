"""Scheduling logic: booking, cancellation, and filling freed slots from the waitlist.

How a canceled slot gets refilled:

    cancel ──▶ outbox: slot_freed ──▶ queue ──▶ consumer: handle_slot_freed
                                                   │
                         first matching waitlist patient gets a *hold* on the slot
                                                   │
                      accept ──▶ booked (their later appointment, if any, is released,
                      │          which frees *that* slot for the next person in line)
                      decline / no reply before the hold expires ──▶ slot_freed again,
                                                                    next patient in line

Every state change and the events it causes are committed in one transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from .clock import Clock
from .config import Settings
from .models import (
    ACTIVE_STATUSES,
    SHOWN_STATUSES,
    Appointment,
    Notification,
    OutboxEvent,
    Patient,
    Provider,
    Slot,
    WaitlistEntry,
)

log = logging.getLogger("carequeue.clinic")
TIMES_OF_DAY = ("any", "morning", "afternoon")


class ClinicError(Exception):
    status = 400


class NotFound(ClinicError):
    status = 404


class Conflict(ClinicError):
    status = 409


def matches_time_of_day(start: datetime, time_of_day: str) -> bool:
    if time_of_day == "morning":
        return start.hour < 12
    if time_of_day == "afternoon":
        return start.hour >= 12
    return True


def fmt_when(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}, {dt.strftime('%I:%M %p').lstrip('0')}"


@dataclass
class Intent:
    """What a patient is looking for, from a form or a natural-language request."""

    provider_id: int | None = None
    specialty: str | None = None
    earliest: date | None = None
    latest: date | None = None
    time_of_day: str = "any"


class Clinic:
    def __init__(self, session_factory: sessionmaker, clock: Clock, settings: Settings, cache, broker):
        self.Session = session_factory
        self.clock = clock
        self.settings = settings
        self.cache = cache
        self.broker = broker

    # ------------------------------------------------------------------ helpers
    def _emit(self, s: Session, type_: str, payload: dict) -> None:
        s.add(OutboxEvent(type=type_, payload=payload, created_at=self.clock.now()))

    def _notify(self, s: Session, patient_id: int, kind: str, message: str) -> None:
        note = Notification(patient_id=patient_id, kind=kind, message=message, created_at=self.clock.now())
        s.add(note)
        s.flush()
        self._emit(s, "notify", {"notification_id": note.id, "patient_id": patient_id, "kind": kind})

    def _invalidate(self, slot: Slot) -> None:
        self.cache.invalidate(slot.provider_id, slot.start.date())

    def _get(self, s: Session, model, id_: int, what: str):
        obj = s.get(model, id_)
        if obj is None:
            raise NotFound(f"{what} {id_} not found")
        return obj

    # ------------------------------------------------------------------ reads
    def providers(self) -> list[dict]:
        with self.Session() as s:
            return [{"id": p.id, "name": p.name, "specialty": p.specialty}
                    for p in s.scalars(select(Provider).order_by(Provider.id))]

    def patients(self) -> list[dict]:
        with self.Session() as s:
            featured = select(Patient).where(Patient.featured.is_(True)).order_by(Patient.name)
            rows = s.scalars(featured).all() or s.scalars(select(Patient).order_by(Patient.name).limit(200)).all()
            return [{"id": p.id, "name": p.name} for p in rows]

    def schedule(self, provider_id: int, day: date) -> dict:
        """Every slot for one provider on one day, with its state. Served from the cache when warm."""
        cached = self.cache.get(provider_id, day)
        if cached is None:
            with self.Session() as s:
                self._get(s, Provider, provider_id, "provider")
                rows = s.execute(
                    select(Slot, Appointment)
                    .outerjoin(Appointment, and_(Appointment.slot_id == Slot.id,
                                                 Appointment.status.in_(SHOWN_STATUSES)))
                    .where(Slot.provider_id == provider_id,
                           Slot.start >= datetime.combine(day, time.min),
                           Slot.start < datetime.combine(day + timedelta(days=1), time.min))
                    .order_by(Slot.start)
                ).all()
                cached = [{"id": sl.id, "start": sl.start.isoformat(), "end": sl.end.isoformat(),
                           "state": appt.status if appt else ("blocked" if sl.blocked_reason else "open"),
                           "blocked_reason": sl.blocked_reason,
                           "patient_id": appt.patient_id if appt else None,
                           "appointment_id": appt.id if appt else None} for sl, appt in rows]
            self.cache.set(provider_id, day, cached)
            source = "database"
        else:
            source = "cache"
        now = self.clock.now()
        slots = [{**sl, "past": datetime.fromisoformat(sl["start"]) <= now} for sl in cached]
        return {"provider_id": provider_id, "date": day.isoformat(), "source": source, "slots": slots}

    def week(self, provider_id: int, start: date, days: int = 7) -> dict:
        """A provider's schedule for the clinic days from `start` (weekends have no slots)."""
        out = [self.schedule(provider_id, start + timedelta(days=i)) for i in range(days)]
        return {"provider_id": provider_id, "start": start.isoformat(),
                "days": [d for d in out if d["slots"]]}

    def slot_story(self, slot_id: int) -> dict:
        """Everything that happened to one slot, oldest first, plus who's next in line for it."""
        now = self.clock.now()
        with self.Session() as s:
            slot = self._get(s, Slot, slot_id, "slot")
            provider = slot.provider
            names = dict(s.execute(select(Patient.id, Patient.name)).all())
            active = s.scalar(select(Appointment).where(Appointment.slot_id == slot_id,
                                                        Appointment.status.in_(ACTIVE_STATUSES)))
            # Slot ids live inside the JSON payload, so filter the recent events in Python
            # (portable across SQLite and PostgreSQL, and cheap at demo scale).
            recent = s.scalars(select(OutboxEvent).where(OutboxEvent.type != "notify")
                               .order_by(OutboxEvent.id.desc()).limit(3000)).all()
            events = [{"type": e.type, "at": e.created_at.isoformat(), "cause": e.payload.get("cause"),
                       "patient": names.get(e.payload.get("patient_id")), "result": e.result}
                      for e in reversed(recent) if e.payload.get("slot_id") == slot_id]
            next_in_line = []
            if slot.start > now and (active is None or active.status == "held"):
                for entry in self._candidates(s, slot):
                    next_in_line.append({"patient": names.get(entry.patient_id), "entry_id": entry.id})
                    if len(next_in_line) == 3:
                        break
            appointment = None
            if active:
                expires = active.hold_expires_at.isoformat() if active.hold_expires_at else None
                appointment = {"id": active.id, "status": active.status, "source": active.source,
                               "patient_id": active.patient_id, "patient": names.get(active.patient_id),
                               "hold_expires_at": expires}
            return {"slot": {"id": slot.id, "start": slot.start.isoformat(), "end": slot.end.isoformat(),
                             "label": fmt_when(slot.start), "provider_id": provider.id, "provider": provider.name,
                             "specialty": provider.specialty, "past": slot.start <= now},
                    "state": active.status if active else "open", "appointment": appointment,
                    "events": events, "next_in_line": next_in_line}

    def open_slots(self, intent: Intent, limit: int = 5) -> list[dict]:
        """Earliest open slots that fit what the patient asked for."""
        now = self.clock.now()
        earliest = max(datetime.combine(intent.earliest, time.min) if intent.earliest else now,
                       now + timedelta(minutes=self.settings.min_notice_minutes))
        q = (select(Slot, Provider).join(Provider)
             .where(Slot.start >= earliest, Slot.blocked_reason.is_(None),
                    ~exists().where(Appointment.slot_id == Slot.id, Appointment.status.in_(ACTIVE_STATUSES)))
             .order_by(Slot.start, Provider.id))
        if intent.latest:
            q = q.where(Slot.start < datetime.combine(intent.latest + timedelta(days=1), time.min))
        if intent.provider_id:
            q = q.where(Slot.provider_id == intent.provider_id)
        elif intent.specialty:
            q = q.where(func.lower(Provider.specialty) == intent.specialty.lower())
        out = []
        with self.Session() as s:
            for slot, prov in s.execute(q.limit(400)):
                if matches_time_of_day(slot.start, intent.time_of_day):
                    out.append({"slot_id": slot.id, "start": slot.start.isoformat(), "provider_id": prov.id,
                                "provider": prov.name, "specialty": prov.specialty, "label": fmt_when(slot.start)})
                    if len(out) >= limit:
                        break
        return out

    def appointments(self, patient_id: int | None = None, include_past: bool = False) -> list[dict]:
        now = self.clock.now()
        with self.Session() as s:
            q = (select(Appointment, Slot, Provider, Patient).join(Slot, Appointment.slot_id == Slot.id)
                 .join(Provider, Slot.provider_id == Provider.id).join(Patient, Appointment.patient_id == Patient.id)
                 .where(Appointment.status.in_(ACTIVE_STATUSES)).order_by(Slot.start))
            if patient_id:
                q = q.where(Appointment.patient_id == patient_id)
            if not include_past:
                q = q.where(Slot.start > now)
            return [{"id": a.id, "status": a.status, "source": a.source, "slot_id": sl.id,
                     "start": sl.start.isoformat(), "label": fmt_when(sl.start),
                     "provider": p.name, "specialty": p.specialty, "patient_id": pt.id, "patient": pt.name,
                     "hold_expires_at": a.hold_expires_at.isoformat() if a.hold_expires_at else None}
                    for a, sl, p, pt in s.execute(q.limit(500))]

    def waitlist(self) -> list[dict]:
        with self.Session() as s:
            rows = s.execute(
                select(WaitlistEntry, Patient, Provider)
                .join(Patient, WaitlistEntry.patient_id == Patient.id)
                .outerjoin(Provider, WaitlistEntry.provider_id == Provider.id)
                .where(WaitlistEntry.status.in_(("waiting", "offered")))
                .order_by(WaitlistEntry.priority.desc(), WaitlistEntry.created_at, WaitlistEntry.id)
            ).all()
            return [{"id": e.id, "patient_id": pt.id, "patient": pt.name, "status": e.status,
                     "provider": p.name if p else None, "specialty": e.specialty or (p.specialty if p else None),
                     "earliest": e.earliest.isoformat(), "latest": e.latest.isoformat(), "time_of_day": e.time_of_day,
                     "created_at": e.created_at.isoformat(), "position": i + 1,
                     "replaces_appointment_id": e.replaces_appointment_id, "priority": e.priority}
                    for i, (e, pt, p) in enumerate(rows)]

    # ------------------------------------------------------------------ writes
    def book(self, patient_id: int, slot_id: int, source: str = "direct") -> dict:
        now = self.clock.now()
        with self.Session() as s:
            slot = self._get(s, Slot, slot_id, "slot")
            patient = self._get(s, Patient, patient_id, "patient")
            if slot.start <= now:
                raise Conflict("That slot has already started.")
            if slot.blocked_reason:
                raise Conflict(f"{slot.provider.name} isn't available at that time.")
            clash = s.scalar(select(Appointment.id).join(Slot).where(
                Appointment.patient_id == patient_id, Appointment.status.in_(ACTIVE_STATUSES),
                Slot.start < slot.end, Slot.end > slot.start))
            if clash:
                raise Conflict(f"{patient.name} already has an appointment at that time.")
            appt = Appointment(slot_id=slot_id, patient_id=patient_id, status="booked", source=source,
                               created_at=now, updated_at=now)
            s.add(appt)
            try:
                s.flush()  # the partial unique index decides who wins a race for the slot
            except IntegrityError as exc:
                s.rollback()
                raise Conflict("Someone else just booked that slot. Please pick another.") from exc
            self._emit(s, "appointment_booked", {"appointment_id": appt.id, "slot_id": slot_id,
                                                 "patient_id": patient_id, "source": source})
            self._notify(s, patient_id, "confirmation",
                         f"Booked with {slot.provider.name} on {fmt_when(slot.start)}.")
            s.commit()
            self._invalidate(slot)
            return {"id": appt.id, "status": appt.status, "slot_id": slot_id, "start": slot.start.isoformat(),
                    "label": fmt_when(slot.start), "provider": slot.provider.name}

    def cancel(self, appointment_id: int, reason: str = "patient") -> dict:
        now = self.clock.now()
        with self.Session() as s:
            appt = self._get(s, Appointment, appointment_id, "appointment")
            if appt.status == "held":
                s.close()
                return self.decline_hold(appointment_id)
            if appt.status != "booked":
                raise Conflict(f"Appointment {appointment_id} is {appt.status}, not booked.")
            appt.status, appt.updated_at, appt.cancel_reason = "cancelled", now, reason
            self._emit(s, "slot_freed", {"slot_id": appt.slot_id, "cause": "cancelled",
                                         "appointment_id": appt.id, "reason": reason})
            if reason == "patient":
                self._notify(s, appt.patient_id, "cancellation",
                             f"Your {fmt_when(appt.slot.start)} appointment is cancelled.")
            s.commit()
            self._invalidate(appt.slot)
            return {"id": appt.id, "status": appt.status}

    def reschedule(self, appointment_id: int, slot_id: int) -> dict:
        """Move a booked appointment to another slot: book the new one first, so the patient
        never ends up with nothing if the new slot was just taken."""
        with self.Session() as s:
            appt = self._get(s, Appointment, appointment_id, "appointment")
            if appt.status != "booked":
                raise Conflict("Only a booked appointment can be moved.")
            patient_id = appt.patient_id
        new = self.book(patient_id, slot_id)
        self.cancel(appointment_id, reason="rescheduled")
        return {**new, "replaced_appointment_id": appointment_id}

    def join_waitlist(self, patient_id: int, earliest: date, latest: date, provider_id: int | None = None,
                      specialty: str | None = None, time_of_day: str = "any",
                      replaces_appointment_id: int | None = None) -> dict:
        if time_of_day not in TIMES_OF_DAY:
            raise ClinicError(f"time_of_day must be one of {', '.join(TIMES_OF_DAY)}")
        if latest < earliest:
            raise ClinicError("The latest date must be on or after the earliest date.")
        if not provider_id and not specialty:
            raise ClinicError("Choose a provider or a specialty.")
        now = self.clock.now()
        with self.Session() as s:
            self._get(s, Patient, patient_id, "patient")
            if provider_id:
                self._get(s, Provider, provider_id, "provider")
            if replaces_appointment_id:
                current = self._get(s, Appointment, replaces_appointment_id, "appointment")
                if current.patient_id != patient_id or current.status != "booked":
                    raise ClinicError("replaces_appointment_id must be one of this patient's booked appointments.")
            entry = WaitlistEntry(patient_id=patient_id, provider_id=provider_id, specialty=specialty,
                                  earliest=earliest, latest=latest, time_of_day=time_of_day, status="waiting",
                                  created_at=now, replaces_appointment_id=replaces_appointment_id)
            s.add(entry)
            s.flush()
            self._emit(s, "waitlist_joined", {"entry_id": entry.id, "patient_id": patient_id})
            s.commit()
            entry_id = entry.id
        # Something may already be open: offer it right away instead of waiting for a cancellation.
        offered = self._offer_open_slot_to(entry_id)
        position = next((w["position"] for w in self.waitlist() if w["id"] == entry_id), None)
        return {"id": entry_id, "status": "offered" if offered else "waiting", "position": position,
                "offered_appointment_id": offered}

    def leave_waitlist(self, entry_id: int) -> dict:
        with self.Session() as s:
            entry = self._get(s, WaitlistEntry, entry_id, "waitlist entry")
            if entry.status not in ("waiting", "offered"):
                raise Conflict(f"Waitlist entry {entry_id} is already {entry.status}.")
            entry.status = "cancelled"
            s.commit()
        return {"id": entry_id, "status": "cancelled"}

    # ------------------------------------------------------------------ holds
    def accept_hold(self, appointment_id: int) -> dict:
        now = self.clock.now()
        with self.Session() as s:
            appt = self._get(s, Appointment, appointment_id, "appointment")
            if appt.status != "held":
                raise Conflict(f"This offer is {appt.status}, so it can't be accepted.")
            if appt.hold_expires_at and appt.hold_expires_at <= now:
                raise Conflict("This offer has expired.")
            appt.status, appt.updated_at, appt.hold_expires_at = "booked", now, None
            released = None
            if appt.waitlist_entry_id:
                entry = s.get(WaitlistEntry, appt.waitlist_entry_id)
                entry.status, entry.booked_at = "booked", now
                released = entry.replaces_appointment_id
            self._emit(s, "hold_accepted", {"appointment_id": appt.id, "slot_id": appt.slot_id,
                                            "patient_id": appt.patient_id})
            self._notify(s, appt.patient_id, "confirmation",
                         f"Confirmed: {appt.slot.provider.name} on {fmt_when(appt.slot.start)}.")
            s.commit()
            self._invalidate(appt.slot)
        if released:
            # Their old, later appointment frees up, which can move the next person up in turn.
            try:
                self.cancel(released, reason="moved_up")
            except ClinicError:
                pass
        return {"id": appointment_id, "status": "booked", "released_appointment_id": released}

    def decline_hold(self, appointment_id: int) -> dict:
        return self._end_hold(appointment_id, "declined")

    def _end_hold(self, appointment_id: int, outcome: str) -> dict:
        now = self.clock.now()
        with self.Session() as s:
            appt = self._get(s, Appointment, appointment_id, "appointment")
            if appt.status != "held":
                raise Conflict(f"This offer is {appt.status}.")
            appt.status, appt.updated_at = outcome, now
            if appt.waitlist_entry_id:
                s.execute(update(WaitlistEntry).where(WaitlistEntry.id == appt.waitlist_entry_id,
                                                      WaitlistEntry.status == "offered").values(status="waiting"))
            self._emit(s, "slot_freed", {"slot_id": appt.slot_id, "cause": outcome, "appointment_id": appt.id})
            s.commit()
            self._invalidate(appt.slot)
        return {"id": appointment_id, "status": outcome}

    def expire_holds(self) -> int:
        """Release holds nobody answered in time; each one goes to the next person in line."""
        now = self.clock.now()
        with self.Session() as s:
            ids = s.scalars(select(Appointment.id).where(Appointment.status == "held",
                                                         Appointment.hold_expires_at <= now)).all()
        expired = 0
        for appt_id in ids:
            try:
                self._end_hold(appt_id, "expired")
                expired += 1
            except Conflict:
                pass  # accepted or declined at the same moment
        return expired

    # ------------------------------------------------------------------ waitlist matching
    def _candidates(self, s: Session, slot: Slot):
        """Waiting patients this slot would suit, first come first served."""
        provider = slot.provider
        prior_offer = exists().where(Appointment.waitlist_entry_id == WaitlistEntry.id,
                                     Appointment.slot_id == slot.id)
        other = aliased(Slot)
        busy = exists().where(Appointment.patient_id == WaitlistEntry.patient_id,
                              Appointment.status.in_(ACTIVE_STATUSES),
                              Appointment.slot_id == other.id, other.start < slot.end, other.end > slot.start)
        q = (select(WaitlistEntry)
             .where(WaitlistEntry.status == "waiting",
                    WaitlistEntry.earliest <= slot.start.date(), WaitlistEntry.latest >= slot.start.date(),
                    or_(WaitlistEntry.provider_id == provider.id,
                        and_(WaitlistEntry.provider_id.is_(None),
                             func.lower(WaitlistEntry.specialty) == provider.specialty.lower())),
                    ~prior_offer, ~busy)
             .order_by(WaitlistEntry.priority.desc(), WaitlistEntry.created_at, WaitlistEntry.id))
        for entry in s.scalars(q):
            if not matches_time_of_day(slot.start, entry.time_of_day):
                continue
            if entry.replaces_appointment_id:
                current = s.get(Appointment, entry.replaces_appointment_id)
                if current is None or current.status != "booked":
                    continue
                if s.get(Slot, current.slot_id).start <= slot.start:
                    continue  # only offer something earlier than what they already have
            yield entry

    def handle_slot_freed(self, slot_id: int) -> str:
        """Offer a freed slot to the first matching waitlisted patient. Safe to run twice."""
        now = self.clock.now()
        with self.Session() as s:
            slot = s.get(Slot, slot_id)
            if slot is None:
                return "slot no longer exists"
            taken = s.scalar(select(Appointment.id).where(Appointment.slot_id == slot_id,
                                                          Appointment.status.in_(ACTIVE_STATUSES)))
            if taken:
                return "already filled"
            if slot.blocked_reason:
                return "doctor unavailable"
            if slot.start <= now + timedelta(minutes=self.settings.min_notice_minutes):
                return "too soon to offer"
            for entry in self._candidates(s, slot):
                claimed = s.execute(update(WaitlistEntry).where(WaitlistEntry.id == entry.id,
                                                                WaitlistEntry.status == "waiting")
                                    .values(status="offered")).rowcount
                if not claimed:
                    continue  # another consumer offered this patient a slot a moment ago
                hold = Appointment(slot_id=slot_id, patient_id=entry.patient_id, status="held", source="waitlist",
                                   created_at=now, updated_at=now, waitlist_entry_id=entry.id,
                                   hold_expires_at=now + timedelta(minutes=self.settings.hold_minutes))
                s.add(hold)
                try:
                    s.flush()
                except IntegrityError:
                    s.rollback()
                    return "already filled"
                self._emit(s, "hold_offered", {"appointment_id": hold.id, "slot_id": slot_id,
                                               "patient_id": entry.patient_id, "entry_id": entry.id})
                self._notify(s, entry.patient_id, "offer",
                             f"An earlier slot opened: {slot.provider.name} on {fmt_when(slot.start)}. "
                             f"Reply within {self.settings.hold_minutes} minutes to take it.")
                s.commit()
                self._invalidate(slot)
                return f"offered to patient {entry.patient_id}"
            return "nobody on the waitlist matches"

    def _offer_open_slot_to(self, entry_id: int) -> int | None:
        """When a patient joins the waitlist, hold the earliest open slot that fits them, if any."""
        with self.Session() as s:
            entry = s.get(WaitlistEntry, entry_id)
            intent = Intent(provider_id=entry.provider_id, specialty=entry.specialty, earliest=entry.earliest,
                            latest=entry.latest, time_of_day=entry.time_of_day)
        for option in self.open_slots(intent, limit=10):
            result = self.handle_slot_freed(option["slot_id"])
            if result == f"offered to patient {entry.patient_id}":
                with self.Session() as s:
                    return s.scalar(select(Appointment.id).where(Appointment.waitlist_entry_id == entry_id,
                                                                 Appointment.status == "held"))
        return None

    # ------------------------------------------------------------------ events
    def publish_pending(self) -> int:
        """Send unpublished outbox events to the queue."""
        with self.Session() as s:
            events = s.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
                               .order_by(OutboxEvent.id).limit(200)).all()
            for ev in events:
                self.broker.publish(ev.id)
                ev.published_at = self.clock.now()
            s.commit()
            return len(events)

    def handle_event(self, event_id: int) -> None:
        """Queue consumer. Idempotent: an event that was already processed is skipped."""
        with self.Session() as s:
            ev = s.get(OutboxEvent, event_id)
            if ev is None or ev.processed_at is not None:
                return
            type_, payload = ev.type, dict(ev.payload)
        if type_ == "slot_freed":
            result = self.handle_slot_freed(payload["slot_id"])
        elif type_ == "notify":
            result = self._deliver(payload["notification_id"])
        else:
            result = "recorded"
        with self.Session() as s:
            s.execute(update(OutboxEvent).where(OutboxEvent.id == event_id, OutboxEvent.processed_at.is_(None))
                      .values(processed_at=self.clock.now(), result=result[:200]))
            s.commit()

    def _deliver(self, notification_id: int) -> str:
        with self.Session() as s:
            note = s.get(Notification, notification_id)
            if note is None:
                return "notification missing"
            if note.sent_at is None:
                note.sent_at = self.clock.now()  # a real deployment would send an SMS or email here
                s.commit()
            return "sent"

    def run_pending(self, max_rounds: int = 50) -> int:
        """Expire holds, publish events and process the queue until nothing is left (tests, demo loop)."""
        total = 0
        for _ in range(max_rounds):
            work = self.expire_holds() + self.publish_pending() + self.broker.drain(self.handle_event)
            total += work
            if not work:
                break
        return total

    # ------------------------------------------------------------------ reporting
    def activity(self, limit: int = 40, provider_id: int | None = None) -> list[dict]:
        with self.Session() as s:
            events = s.scalars(select(OutboxEvent).where(OutboxEvent.type != "notify")
                               .order_by(OutboxEvent.id.desc()).limit(limit if provider_id is None else 1500)).all()
            names = dict(s.execute(select(Patient.id, Patient.name)).all())
            slots = {sl.id: sl for sl in s.scalars(select(Slot).where(
                Slot.id.in_({e.payload.get("slot_id") for e in events if e.payload.get("slot_id")})))}
            out = []
            for e in events:
                slot = slots.get(e.payload.get("slot_id"))
                if provider_id is not None and (slot is None or slot.provider_id != provider_id):
                    continue
                if len(out) >= limit:
                    break
                out.append({"id": e.id, "type": e.type, "at": e.created_at.isoformat(),
                            "patient": names.get(e.payload.get("patient_id")),
                            "slot": fmt_when(slot.start) if slot else None,
                            "provider": slot.provider.name if slot else None,
                            "cause": e.payload.get("cause"), "result": e.result,
                            "processed": e.processed_at is not None})
            return out

    def notifications(self, patient_id: int | None = None, limit: int = 30) -> list[dict]:
        with self.Session() as s:
            q = select(Notification, Patient).join(Patient).order_by(Notification.id.desc()).limit(limit)
            if patient_id:
                q = q.where(Notification.patient_id == patient_id)
            return [{"id": n.id, "patient": p.name, "kind": n.kind, "message": n.message,
                     "at": n.created_at.isoformat(), "sent": n.sent_at is not None} for n, p in s.execute(q)]

    def metrics(self, days: int = 7) -> dict:
        now = self.clock.now()
        end = now + timedelta(days=days)
        with self.Session() as s:
            total = s.scalar(select(func.count(Slot.id)).where(Slot.start > now, Slot.start <= end,
                                                               Slot.blocked_reason.is_(None))) or 0
            filled = s.scalar(select(func.count(Appointment.id)).join(Slot).where(
                Slot.start > now, Slot.start <= end, Appointment.status == "booked")) or 0
            held = s.scalar(select(func.count(Appointment.id)).join(Slot).where(
                Slot.start > now, Slot.start <= end, Appointment.status == "held")) or 0
            counts = dict(s.execute(select(Appointment.status, func.count()).where(Appointment.source == "waitlist")
                                    .group_by(Appointment.status)).all())
            cancelled = s.scalar(select(func.count()).select_from(Appointment)
                                 .where(Appointment.status == "cancelled")) or 0
            waiting = s.scalar(select(func.count()).select_from(WaitlistEntry)
                               .where(WaitlistEntry.status.in_(("waiting", "offered")))) or 0
        offers = sum(counts.values())
        accepted = counts.get("booked", 0) + counts.get("cancelled", 0)
        return {
            "window_days": days, "slots": total, "booked": filled, "held": held,
            "utilization": round(filled / total, 4) if total else 0.0,
            "cancellations": cancelled, "waitlist_size": waiting,
            "offers": {"made": offers, "accepted": accepted, "pending": counts.get("held", 0),
                       "declined": counts.get("declined", 0), "expired": counts.get("expired", 0)},
            # Of the offers patients have answered (or let expire); pending ones aren't counted yet.
            "acceptance_rate": round(accepted / decided, 4) if (decided := offers - counts.get("held", 0)) else 0.0,
            "cache": {"backend": self.cache.backend, "hits": self.cache.hits, "misses": self.cache.misses},
            "queue": {"backend": self.broker.kind},
        }
