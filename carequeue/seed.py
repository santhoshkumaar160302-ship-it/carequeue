"""Demo data: four doctors with working hours, patients and their accounts, eight weeks of
past visits (with notes), two weeks of bookings ahead and a waitlist.

Everything is generated relative to "now", so the demo always looks current. Slot
creation is idempotent, so running it again only adds days that don't exist yet.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import seed_users
from .availability import Availability, Day
from .clinic import fmt_when
from .models import Appointment, Notification, Patient, Provider, Slot, TimeOff, WaitlistEntry, WorkingHours

PROVIDERS = [
    ("Dr. Maya Patel", "General Practice"),
    ("Dr. James Okafor", "Pediatrics"),
    ("Dr. Lena Brooks", "Dermatology"),
    ("Dr. Arjun Mehta", "Cardiology"),
]
PATIENTS = [
    "Ava Thompson", "Noah Garcia", "Mia Robinson", "Liam Chen", "Sofia Martinez", "Ethan Walker",
    "Isabella Nguyen", "Lucas Hall", "Amelia Young", "Mason Wright", "Harper Lopez", "Elijah King",
    "Evelyn Scott", "Logan Green", "Abigail Adams", "Aiden Baker", "Emily Nelson", "Jackson Carter",
    "Ella Mitchell", "Sebastian Perez", "Grace Roberts", "Henry Turner", "Chloe Phillips", "Owen Campbell",
]
# Everyone else at the clinic: enough people that nobody has a dozen visits in two weeks.
FIRST = ["Olivia", "James", "Zoe", "Daniel", "Layla", "Samuel", "Nora", "David", "Ruby", "Leo", "Hazel", "Isaac",
         "Aria", "Caleb", "Stella", "Ryan", "Lucy", "Nathan", "Maya", "Adrian"]
LAST = ["Foster", "Reyes", "Patterson", "Kim", "Hughes", "Ortiz", "Price", "Bennett", "Wood", "Sanders", "Ross",
        "Morales", "Powell", "Sullivan", "Russell", "Jenkins", "Perry", "Long", "Butler", "Barnes"]
MAX_PERSONA_VISITS = 2
MAX_PERSONA_PAST = 4
HISTORY_DAYS = 56
NOTES = {
    "General Practice": ["Annual check-up. Blood pressure 122/80, all normal. Repeat bloods in 12 months.",
                         "Seasonal cold, no signs of infection. Rest and fluids; come back if not better in a week.",
                         "Bloods: cholesterol slightly high. Diet changes discussed; recheck in 3 months.",
                         "Lower back strain. Gentle exercises given; ibuprofen as needed for 5 days.",
                         "Flu vaccine given. No reaction during the 15-minute wait."],
    "Pediatrics": ["Well-child visit. Growth on track, vaccinations up to date.",
                   "Ear infection on the left side. Amoxicillin for 7 days.",
                   "Mild eczema on both arms. Moisturise twice a day; review in 4 weeks.",
                   "Sports physical. Fit to play."],
    "Dermatology": ["Mole check: 3 moles reviewed, none concerning. Photo record updated.",
                    "Acne improving on the current cream. Continue for 8 more weeks.",
                    "Contact dermatitis from a new detergent. Hydrocortisone 1% for 7 days.",
                    "Removed a small skin tag from the neck. Healing well."],
    "Cardiology": ["ECG normal. Heart rate 68. Keep up the walking; review in 6 months.",
                   "Blood pressure 138/88. Home readings for 2 weeks, then a follow-up.",
                   "Echo results reviewed: normal valve function.",
                   "Palpitations after coffee. Holter monitor booked; cut caffeine meanwhile."],
}


def clinic_days(start: date, days: int) -> list[date]:
    return [d for d in (start + timedelta(days=i) for i in range(days)) if d.weekday() < 5]


def ensure_slots(session, start: date, days: int, open_hour: int = 9, close_hour: int = 17,
                 slot_minutes: int = 30) -> int:
    providers = session.scalars(select(Provider)).all()
    rows = []
    for day in clinic_days(start, days):
        t = datetime.combine(day, time(open_hour))
        while t + timedelta(minutes=slot_minutes) <= datetime.combine(day, time(close_hour)):
            rows.extend({"provider_id": p.id, "start": t, "end": t + timedelta(minutes=slot_minutes)}
                        for p in providers)
            t += timedelta(minutes=slot_minutes)
    if not rows:
        return 0
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(Slot).values(rows).on_conflict_do_nothing(index_elements=["provider_id", "start"])
    return session.execute(stmt).rowcount or 0


def seed_demo(session, now: datetime, horizon_days: int = 14, fill: float = 0.7, seed: int = 7, **slot_kw) -> bool:
    """Create the demo clinic if the database is empty. Returns True if it seeded."""
    if session.scalar(select(func.count(Provider.id))):
        if ensure_slots(session, now.date(), horizon_days, **slot_kw):
            apply_hours(session)
        session.commit()
        return False
    rng = random.Random(seed)
    session.add_all(Provider(name=n, specialty=s) for n, s in PROVIDERS)
    session.add_all(Patient(name=n, phone=f"555-01{i:02d}", featured=True) for i, n in enumerate(PATIENTS))
    session.add_all(Patient(name=f"{f} {last}", phone=f"555-2{i:03d}") for i, (f, last) in
                    enumerate((f, last) for last in LAST for f in FIRST))
    session.flush()
    # Every doctor works Monday to Friday, 9:00-17:00; they can change it from their portal.
    for p in session.scalars(select(Provider)):
        session.add_all(WorkingHours(provider_id=p.id, weekday=d, start_minute=9 * 60, end_minute=17 * 60)
                        for d in range(5))
    ensure_slots(session, now.date() - timedelta(days=HISTORY_DAYS), HISTORY_DAYS, **slot_kw)
    ensure_slots(session, now.date(), horizon_days, **slot_kw)
    apply_hours(session)
    personas = session.scalars(select(Patient).where(Patient.featured.is_(True))).all()
    others = session.scalars(select(Patient).where(Patient.featured.is_(False))).all()
    demo = next((p for p in personas if p.name == "Harper Lopez"), None)
    busy: set[tuple[int, datetime]] = set()
    _seed_history(session, rng, now, personas, others, demo, busy)
    visits: dict[int, int] = {}
    waitlisted = rng.sample([p for p in personas if p is not demo], 6)
    patients = others
    for slot in session.scalars(select(Slot).where(Slot.start > now, Slot.blocked_reason.is_(None))
                                .order_by(Slot.start)):
        # Like a real clinic: the next few days are fully booked (that's why people join the
        # waitlist), the rest of the week is busy, and the week after is partly open.
        days_out = (slot.start.date() - now.date()).days
        chance = 1.0 if days_out <= 3 else 0.85 if days_out <= 7 else fill - 0.3
        if rng.random() < chance:
            # Now and then a demo persona, so each has an appointment or two to try things on.
            pool = personas if rng.random() < 0.08 else patients
            for patient in rng.sample(pool, len(pool)):
                if ((patient.id, slot.start) not in busy and patient not in waitlisted
                        and (not patient.featured or visits.get(patient.id, 0) < MAX_PERSONA_VISITS)):
                    break
            else:
                patient = rng.choice(patients)
            visits[patient.id] = visits.get(patient.id, 0) + 1
            busy.add((patient.id, slot.start))
            session.add(Appointment(slot_id=slot.id, patient_id=patient.id, status="booked", source="direct",
                                    created_at=now, updated_at=now))
    providers = session.scalars(select(Provider)).all()
    if demo is not None:
        _give_demo_patient_a_visit(session, now, demo, providers[0], busy)
    for i, patient in enumerate(waitlisted):
        provider = providers[i % len(providers)]
        session.add(WaitlistEntry(patient_id=patient.id, provider_id=provider.id, earliest=now.date(),
                                  latest=now.date() + timedelta(days=3),
                                  time_of_day=("any", "morning", "afternoon")[i % 3],
                                  status="waiting", created_at=now - timedelta(hours=6 - i)))
    session.commit()
    seed_users(session, now)
    return True


def apply_hours(session) -> None:
    """Mark slots outside each doctor's hours (or inside their time off) as unavailable.
    Used when slots are created; later changes go through Availability.reconcile."""
    hours: dict[int, dict[int, Day]] = {}
    for h in session.scalars(select(WorkingHours)):
        hours.setdefault(h.provider_id, {})[h.weekday] = Day(h.weekday, h.start_minute, h.end_minute)
    offs: dict[int, list] = {}
    for t in session.scalars(select(TimeOff)):
        offs.setdefault(t.provider_id, []).append((t.start, t.end))
    for slot in session.scalars(select(Slot).where(Slot.blocked_reason.is_(None))):
        slot.blocked_reason = Availability._blocked_reason(slot, hours.get(slot.provider_id),
                                                           offs.get(slot.provider_id, []))
    session.flush()


def _seed_history(session, rng, now, personas, others, demo, busy) -> None:
    """Eight weeks of past visits. The visits of the last hour stay 'booked' for the doctor to close."""
    past_visits: dict[int, int] = {}
    start_of_today = datetime.combine(now.date(), time.min)
    for slot in session.scalars(select(Slot).where(Slot.start <= now, Slot.blocked_reason.is_(None))
                                .order_by(Slot.start)):
        if rng.random() > 0.8:
            continue
        pool = personas if rng.random() < 0.12 else others
        if slot.provider_id == 1 and demo is not None and past_visits.get(demo.id, 0) < 3 and rng.random() < 0.02:
            patient = demo
        else:
            patient = next((p for p in rng.sample(pool, len(pool)) if (p.id, slot.start) not in busy and
                            (not p.featured or past_visits.get(p.id, 0) < MAX_PERSONA_PAST)), rng.choice(others))
        past_visits[patient.id] = past_visits.get(patient.id, 0) + 1
        busy.add((patient.id, slot.start))
        if slot.start >= start_of_today and slot.end > now - timedelta(hours=1):
            status, note = "booked", None  # the last hour of today's visits, still to be written up
        else:
            roll = rng.random()
            status = "completed" if roll < 0.9 else "no_show" if roll < 0.95 else "cancelled"
            note = rng.choice(NOTES[slot.provider.specialty]) if status == "completed" else None
        made = slot.start - timedelta(days=rng.randint(2, 20))
        session.add(Appointment(slot_id=slot.id, patient_id=patient.id, status=status, source="direct",
                                created_at=made, updated_at=slot.end if status != "cancelled" else made,
                                note=note, cancel_reason="patient" if status == "cancelled" else None))
    session.flush()


def _give_demo_patient_a_visit(session, now, demo, provider, busy) -> None:
    """Make sure the demo patient has something coming up with the demo doctor."""
    has_one = session.scalar(select(func.count(Appointment.id)).join(Slot).where(
        Appointment.patient_id == demo.id, Appointment.status == "booked", Slot.start > now,
        Slot.provider_id == provider.id))
    if has_one:
        return
    session.flush()
    taken = select(Appointment.slot_id).where(Appointment.status.in_(("booked", "held")))
    for slot in session.scalars(select(Slot).where(
            Slot.provider_id == provider.id, Slot.blocked_reason.is_(None),
            Slot.start > now + timedelta(days=2), ~Slot.id.in_(taken)).order_by(Slot.start)):
        if (demo.id, slot.start) not in busy and 10 <= slot.start.hour < 16:
            session.add(Appointment(slot_id=slot.id, patient_id=demo.id, status="booked", source="direct",
                                    created_at=now, updated_at=now))
            session.add(Notification(patient_id=demo.id, kind="confirmation", created_at=now, sent_at=now,
                                     message=f"Booked with {provider.name} on {fmt_when(slot.start)}."))
            return

