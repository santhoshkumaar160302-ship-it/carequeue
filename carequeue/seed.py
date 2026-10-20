"""Demo data: four doctors with working hours, patients and their accounts, eight weeks of
past visits (with notes), two weeks of bookings ahead and a waitlist.

Everything is generated relative to "now", so the demo always looks current. Slot
creation is idempotent, so running it again only adds days that don't exist yet.
"""
from __future__ import annotations

import json
import random
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import seed_users
from .availability import Availability, Day
from .clinic import fmt_when
from .models import (
    Allergy,
    Appointment,
    Condition,
    Medication,
    Notification,
    Patient,
    Provider,
    Slot,
    TimeOff,
    Vitals,
    WaitlistEntry,
    WorkingHours,
)

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
                 slot_minutes: int = 30, every_day: bool | None = None) -> int:
    """Create the slot grid. The app's grid runs around the clock on every day (0-24); doctors' shifts
    decide which slots are bookable. Tests use a smaller weekday grid (9-17, Monday to Friday)."""
    providers = session.scalars(select(Provider)).all()
    rows = []
    if every_day is None:
        every_day = (open_hour, close_hour) == (0, 24)
    days_list = [start + timedelta(days=i) for i in range(days)] if every_day else clinic_days(start, days)
    for day in days_list:
        t = datetime.combine(day, time.min) + timedelta(hours=open_hour)
        while t + timedelta(minutes=slot_minutes) <= datetime.combine(day, time.min) + timedelta(hours=close_hour):
            rows.extend({"provider_id": p.id, "start": t, "end": t + timedelta(minutes=slot_minutes)}
                        for p in providers)
            t += timedelta(minutes=slot_minutes)
    if not rows:
        return 0
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(Slot).values(rows).on_conflict_do_nothing(index_elements=["provider_id", "start"])
    return session.execute(stmt).rowcount or 0


def seed_demo(session, now: datetime, horizon_days: int = 14, fill: float = 0.7, seed: int = 7,
              demo_password: str | None = None, **slot_kw) -> bool:
    """Create the demo clinic if the database is empty. Returns True if it seeded."""
    if session.scalar(select(func.count(Provider.id))):
        seed_users(session, now, demo_password)  # keeps the sample accounts' password in step with the setting
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
    # Every doctor works the day shift, Monday to Friday, and can change it from their portal
    # (morning, day, evening or night shifts, or custom hours, any day of the week).
    for p in session.scalars(select(Provider)):
        session.add_all(WorkingHours(provider_id=p.id, weekday=d, start_minute=9 * 60, end_minute=17 * 60)
                        for d in range(5))
    # Dr. Okafor (pediatrics) also does a Saturday morning shift, so weekends aren't empty.
    okafor = session.scalar(select(Provider.id).where(Provider.name == "Dr. James Okafor"))
    session.add(WorkingHours(provider_id=okafor, weekday=5, start_minute=6 * 60, end_minute=14 * 60))
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
    seed_charts(session, now, rng)
    seed_users(session, now, demo_password)
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
    # The doctor's write-up pile: the last hour of visits before now, on the most recent clinic day
    # (today's, or Friday's when it's the weekend or before opening), so there's always one to try.
    # Each doctor's own last working day: Dr. Okafor's Saturday shift shouldn't empty Dr. Patel's pile.
    last_end = dict(session.execute(select(Slot.provider_id, func.max(Slot.end)).where(
        Slot.start <= now, Slot.blocked_reason.is_(None)).group_by(Slot.provider_id)).all())
    anchors = {pid: min(now, end) for pid, end in last_end.items()}
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
        anchor = anchors.get(slot.provider_id, now)
        if slot.start.date() == anchor.date() and slot.end > anchor - timedelta(hours=1):
            status, note = "booked", None  # still to be written up
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



# ---------------------------------------------------------------------------- charts
CHARTS_FILE = Path(__file__).resolve().parent / "data" / "synthea_charts.json"
FEMALE = {"Ava", "Mia", "Sofia", "Isabella", "Amelia", "Harper", "Evelyn", "Abigail", "Emily", "Ella", "Grace",
          "Chloe", "Olivia", "Zoe", "Layla", "Nora", "Ruby", "Hazel", "Aria", "Stella", "Lucy", "Maya"}


def seed_charts(session, now: datetime, rng: random.Random, path: Path = CHARTS_FILE) -> int:
    """Give patients realistic synthetic histories generated by Synthea (see scripts/import_synthea.py).

    Charts go to the demo personas first, then to the patients who visit most, with
    children's charts going to the patients who mostly see the pediatrician. Synthea's
    dates are relative, so they're shifted to end around today. Each completed visit of a
    charted patient also gets a set of vitals close to their last Synthea reading, so a
    doctor sees a believable trend.
    """
    if not path.is_file():
        return 0
    charts = json.loads(path.read_text())["charts"]
    today = now.date()
    adults = [c for c in charts if c["birth_day"] <= -18 * 365]
    kids = [c for c in charts if c["birth_day"] > -18 * 365]

    def richness(c: dict) -> tuple:
        return (bool(c["allergies"]), sum(m["status"] == "active" for m in c["medications"]), len(c["conditions"]))

    def sex_of(p: Patient) -> str:
        return "female" if p.name.split()[0] in FEMALE else "male"

    def take(pool: list[dict], sex: str, best: bool = False) -> dict | None:
        options = [c for c in pool if c["sex"] == sex] or pool
        if not options:
            return None
        chosen = max(options, key=richness) if best else options[0]
        pool.remove(chosen)
        return chosen

    peds = session.scalar(select(Provider.id).where(Provider.specialty == "Pediatrics"))
    visits = Counter()
    ped_visits = Counter()
    for patient_id, provider_id in session.execute(
            select(Appointment.patient_id, Slot.provider_id).join(Slot, Appointment.slot_id == Slot.id)):
        visits[patient_id] += 1
        ped_visits[patient_id] += provider_id == peds
    personas = session.scalars(select(Patient).where(Patient.featured.is_(True)).order_by(Patient.id)).all()
    demo = next((p for p in personas if p.name == "Harper Lopez"), None)
    others = session.scalars(select(Patient).where(Patient.featured.is_(False))).all()
    rng.shuffle(adults)
    rng.shuffle(kids)

    assigned: list[tuple[Patient, dict]] = []
    if demo is not None:
        assigned.append((demo, take(adults, "female", best=True)))  # the demo patient has a full chart
    for p in personas:
        if p is not demo and (chart := take(adults, sex_of(p))):
            assigned.append((p, chart))
    by_peds = sorted((p for p in others if ped_visits[p.id]), key=lambda p: (-ped_visits[p.id], p.id))
    for p in by_peds:
        if not kids:
            break
        assigned.append((p, take(kids, sex_of(p))))
    # The rest go to the demo doctor's next patients, so the charts show up where people look first.
    demo_doctor = session.scalar(select(Provider.id).where(Provider.name == "Dr. Maya Patel"))
    soonest: dict[int, datetime] = {}
    for patient_id, start in session.execute(
            select(Appointment.patient_id, Slot.start).join(Slot, Appointment.slot_id == Slot.id)
            .where(Slot.provider_id == demo_doctor, Appointment.status == "booked",
                   Slot.end > now - timedelta(hours=1)).order_by(Slot.start)):
        soonest.setdefault(patient_id, start)
    far = now + timedelta(days=365)
    taken = {p.id for p, _ in assigned}
    for p in sorted((p for p in others if p.id not in taken),
                    key=lambda p: (soonest.get(p.id, far), -visits[p.id], p.id)):
        if not adults:
            break
        assigned.append((p, take(adults, sex_of(p))))

    def on(day: int | None) -> date | None:
        return today + timedelta(days=day) if day is not None else None

    for patient, chart in assigned:
        patient.birth_date, patient.sex = on(chart["birth_day"]), chart["sex"]
        for a in chart["allergies"]:
            session.add(Allergy(patient_id=patient.id, substance=a["substance"], code=a["code"],
                                category=a["category"], criticality=a["criticality"], reaction=a["reaction"],
                                status=a["status"], recorded_on=on(a["recorded_day"]), source="synthea"))
        for c in chart["conditions"]:
            session.add(Condition(patient_id=patient.id, name=c["name"], code=c["code"], status=c["status"],
                                  onset=on(c["onset_day"]), resolved=on(c["resolved_day"]), source="synthea"))
        for m in chart["medications"]:
            session.add(Medication(patient_id=patient.id, name=m["name"], code=m["code"], status=m["status"],
                                   started=on(m["start_day"]),
                                   stopped=on(m["last_day"]) if m["status"] == "stopped" else None,
                                   source="synthea"))
        for v in chart["vitals"]:
            session.add(Vitals(patient_id=patient.id, taken_at=datetime.combine(on(v["day"]), time(10)),
                               **{k: v.get(k) for k in ("systolic", "diastolic", "heart_rate", "temperature_c",
                                                        "weight_kg", "height_cm", "bmi")}, source="synthea"))
        _vitals_at_visits(session, patient, chart, rng, now)
    session.commit()
    return len(assigned)


def _vitals_at_visits(session, patient: Patient, chart: dict, rng: random.Random, now: datetime) -> None:
    """Readings for this patient's completed CareQueue visits, close to their last Synthea values."""
    last = {}
    for v in chart["vitals"]:
        last.update({k: val for k, val in v.items() if val is not None})
    if "systolic" not in last:
        return
    done = session.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id)
                           .where(Appointment.patient_id == patient.id, Appointment.status == "completed",
                                  Slot.start <= now).order_by(Slot.start)).all()
    days = set()
    for appt, slot in done:
        if slot.start.date() in days:
            continue  # two visits on one day: vitals are taken once
        days.add(slot.start.date())
        systolic = round(last["systolic"] + rng.uniform(-6, 6))
        session.add(Vitals(
            patient_id=patient.id, appointment_id=appt.id, taken_at=slot.start, source="clinic",
            recorded_by=slot.provider_id, systolic=systolic,
            diastolic=min(round(last.get("diastolic", 80) + rng.uniform(-4, 4)), systolic - 20),
            heart_rate=round(last.get("heart_rate", 75) + rng.uniform(-6, 6)),
            temperature_c=round(rng.uniform(36.4, 37.2), 1),
            weight_kg=(weight := round(last["weight_kg"] + rng.uniform(-0.8, 0.8), 1) if "weight_kg" in last else None),
            height_cm=last.get("height_cm"),
            bmi=round(weight / (last["height_cm"] / 100) ** 2, 1) if weight and last.get("height_cm") else None))
