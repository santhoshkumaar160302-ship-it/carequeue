"""Doctor availability (hours, time off, rebooking displaced patients) and visit records."""
from datetime import date, datetime, timedelta

import pytest

from carequeue.availability import Availability
from carequeue.clinic import ClinicError, Conflict, NotFound
from carequeue.visits import Visits

from .conftest import PATIENTS
from .helpers import appointment, held_for, slot_at

WEEKDAYS_9_TO_5 = [{"weekday": d, "start": "09:00", "end": "17:00"} for d in range(5)]
WED = date(2026, 10, 14)
WED_10 = datetime(2026, 10, 14, 10, 0)
THU_10 = datetime(2026, 10, 15, 10, 0)


@pytest.fixture
def avail(clinic):
    a = Availability(clinic)
    a.set_hours(1, WEEKDAYS_9_TO_5)
    return a


@pytest.fixture
def visits(clinic):
    return Visits(clinic)


def states(clinic, day, provider_id=1):
    return {datetime.fromisoformat(s["start"]).strftime("%H:%M"): s["state"]
            for s in clinic.schedule(provider_id, day)["slots"]}


def test_shorter_hours_block_slots_and_they_cannot_be_booked(clinic, avail):
    days = [*WEEKDAYS_9_TO_5[:4], {"weekday": 4, "start": "09:00", "end": "12:00"}]
    preview = avail.preview_hours(1, days)
    assert preview["blocked"] == 10 and preview["affected"] == []
    avail.set_hours(1, days)
    friday = states(clinic, date(2026, 10, 16))
    assert friday["11:30"] == "open" and friday["12:00"] == "blocked" and friday["16:30"] == "blocked"
    with pytest.raises(Conflict, match="isn't available"):
        clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, datetime(2026, 10, 16, 14, 0)))
    assert all(datetime.fromisoformat(o["start"]).hour < 12 or datetime.fromisoformat(o["start"]).weekday() != 4
               for o in clinic.open_slots(_intent(1), limit=200))
    assert avail.hours(1)[4] == {"weekday": 4, "name": "Friday", "working": True, "start": "09:00",
                                       "end": "12:00", "shift": "custom", "overnight": False}


def _intent(provider_id):
    from carequeue.clinic import Intent
    return Intent(provider_id=provider_id, specialty=None, earliest=None, latest=None, time_of_day="any")


def test_time_off_displaces_patients_to_the_front_of_the_line(clinic, avail, visits, clock):
    booked = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, WED_10))
    # Mia was already waiting for Dr. Patel; Ava jumps ahead of her because the doctor cancelled on her.
    clinic.join_waitlist(PATIENTS["Mia"], WED, date(2026, 10, 16), provider_id=1)
    clinic.run_pending()
    for a in held_for(clinic, PATIENTS["Mia"]):
        clinic.decline_hold(a["id"])
    clinic.run_pending()
    preview = avail.preview_time_off(1, WED_10, WED_10 + timedelta(hours=2))
    assert [a["patient_id"] for a in preview["affected"]] == [PATIENTS["Ava"]]
    result = avail.add_time_off(1, WED_10, WED_10 + timedelta(hours=2), "Training")
    clinic.run_pending()
    assert appointment(clinic, booked["id"]).status == "cancelled"
    assert appointment(clinic, booked["id"]).cancel_reason == "doctor_unavailable"
    assert result["blocked"] == 4 and result["rebooking"][0]["patient"] == "Ava"
    offer = held_for(clinic, PATIENTS["Ava"])
    assert offer, "Ava is offered a new slot straight away"
    assert visits.overview(PATIENTS["Ava"])["waitlist"][0]["priority"] == 1
    assert any("can't see you" in n["message"] for n in visits.overview(PATIENTS["Ava"])["notifications"])
    assert states(clinic, WED)["10:30"] == "blocked"
    assert avail.time_off(1)[0]["reason"] == "Training"


def test_removing_time_off_reopens_slots_and_offers_them(clinic, avail):
    off = avail.add_time_off(1, datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 15, 17, 0))
    for slot in clinic.schedule(1, date(2026, 10, 14))["slots"]:
        if slot["state"] == "open":
            clinic.book(PATIENTS["Liam"], slot["id"])
    clinic.join_waitlist(PATIENTS["Noah"], date(2026, 10, 15), date(2026, 10, 15), provider_id=1)
    assert held_for(clinic, PATIENTS["Noah"]) == []  # Thursday is blocked, nothing to offer yet
    result = avail.remove_time_off(1, off["id"])
    clinic.run_pending()
    assert result["opened"] == 16
    assert held_for(clinic, PATIENTS["Noah"]), "the reopened Thursday is offered to the waitlist"


def test_blocking_a_pending_offer_withdraws_it(clinic, avail):
    clinic.join_waitlist(PATIENTS["Sofia"], date(2026, 10, 15), date(2026, 10, 15), provider_id=1)
    hold = held_for(clinic, PATIENTS["Sofia"])[0]
    avail.add_time_off(1, datetime(2026, 10, 15, 0, 0), datetime(2026, 10, 16, 0, 0), "Day off")
    assert appointment(clinic, hold["id"]).status == "cancelled"
    assert clinic.waitlist()[0]["status"] == "waiting"


def test_hours_and_time_off_validation(clinic, avail):
    with pytest.raises(ClinicError, match="at least one"):
        avail.set_hours(1, [])
    with pytest.raises(ClinicError, match="must differ"):
        avail.set_hours(1, [{"weekday": 0, "start": "09:00", "end": "09:00"}])
    with pytest.raises(ClinicError, match="Shift must be"):
        avail.set_hours(1, [{"weekday": 0, "shift": "graveyard"}])
    with pytest.raises(ClinicError, match="09:00"):
        avail.set_hours(1, [{"weekday": 0, "start": "9am", "end": "17:00"}])
    with pytest.raises(ClinicError, match="end after"):
        avail.add_time_off(1, THU_10, THU_10)
    with pytest.raises(NotFound):
        avail.remove_time_off(2, avail.add_time_off(1, THU_10, THU_10 + timedelta(hours=1))["id"])


def test_doctor_closes_visits_with_notes_only_after_they_start(clinic, visits, clock):
    booked = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, datetime(2026, 10, 12, 9, 0)))
    with pytest.raises(Conflict, match="once it has started"):
        visits.close(1, booked["id"], "completed")
    clock.advance(hours=1, minutes=5)
    assert visits.today(1)["counts"]["to_close"] == 1
    with pytest.raises(NotFound):
        visits.close(2, booked["id"], "completed", "not my patient")  # another doctor can't touch it
    done = visits.close(1, booked["id"], "completed", "  All good.  ")
    assert done["status"] == "completed" and done["note"] == "All good."
    assert visits.set_note(1, booked["id"], "All good. Recheck in 6 months.")["note"].endswith("months.")
    history = visits.history(PATIENTS["Ava"])
    assert history[0]["status"] == "completed" and history[0]["provider"] == "Dr. Maya Patel"
    assert clinic.schedule(1, date(2026, 10, 12))["slots"][0]["state"] == "completed"


def test_doctor_sees_next_patient_and_their_last_visit(clinic, visits, clock):
    first = clinic.book(PATIENTS["Noah"], slot_at(clinic, 1, datetime(2026, 10, 12, 9, 0)))
    clinic.book(PATIENTS["Noah"], slot_at(clinic, 1, datetime(2026, 10, 12, 11, 0)))
    clinic.book(PATIENTS["Mia"], slot_at(clinic, 2, datetime(2026, 10, 12, 10, 0)))
    clock.advance(hours=1, minutes=40)
    visits.close(1, first["id"], "completed", "Sore throat.")
    today = visits.today(1)
    assert today["next"]["patient"] == "Noah" and today["next"]["last_visit"]["note"] == "Sore throat."
    patients = visits.doctor_patients(1)
    assert [p["name"] for p in patients] == ["Noah"] and patients[0]["visits"] == 1
    detail = visits.doctor_patient(1, PATIENTS["Noah"])
    assert len(detail["upcoming"]) == 1 and detail["history"][0]["note"] == "Sore throat."
    with pytest.raises(NotFound):
        visits.doctor_patient(1, PATIENTS["Mia"])  # Mia only sees Dr. Brooks


def test_patient_week_never_shows_other_patients(clinic, visits, avail):
    mine = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, WED_10))
    clinic.book(PATIENTS["Noah"], slot_at(clinic, 1, THU_10))
    avail.add_time_off(1, datetime(2026, 10, 16, 15, 0), datetime(2026, 10, 16, 17, 0))
    week = visits.patient_week(PATIENTS["Ava"], 1, date(2026, 10, 12))
    slots = [s for d in week["days"] for s in d["slots"]]
    assert {s["state"] for s in slots} <= {"open", "taken", "mine", "offer", "unavailable"}
    assert [s["appointment_id"] for s in slots if s["state"] == "mine"] == [mine["id"]]
    assert all(s["patient_id"] in (None, PATIENTS["Ava"]) for s in slots)
    assert any(s["state"] == "unavailable" for s in slots)  # Friday afternoon off


def test_reschedule_books_the_new_slot_before_releasing_the_old_one(clinic):
    old = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, THU_10))
    taken = clinic.book(PATIENTS["Noah"], slot_at(clinic, 1, WED_10))
    with pytest.raises(Conflict):
        clinic.reschedule(old["id"], taken["slot_id"])
    assert appointment(clinic, old["id"]).status == "booked"  # nothing lost when the move fails
    moved = clinic.reschedule(old["id"], slot_at(clinic, 1, datetime(2026, 10, 14, 11, 0)))
    assert moved["replaced_appointment_id"] == old["id"]
    assert appointment(clinic, old["id"]).cancel_reason == "rescheduled"


def test_doctor_can_split_patients_into_seen_and_new(clinic, visits, clock):
    seen = clinic.book(PATIENTS["Noah"], slot_at(clinic, 1, datetime(2026, 10, 12, 9, 0)))
    clinic.book(PATIENTS["Mia"], slot_at(clinic, 1, THU_10))  # booked, never seen
    clinic.join_waitlist(PATIENTS["Sofia"], WED, date(2026, 10, 16), provider_id=1)  # only waiting
    for a in held_for(clinic, PATIENTS["Sofia"]):
        clinic.decline_hold(a["id"])  # keep her waiting, with no appointment
    clock.advance(hours=1, minutes=5)
    visits.close(1, seen["id"], "completed", "Fine.")

    groups = {p["name"]: p["group"] for p in visits.doctor_patients(1)}
    assert groups == {"Noah": "mine", "Mia": "new", "Sofia": "new"}
    assert [p["name"] for p in visits.doctor_patients(1, group="mine")] == ["Noah"]
    new = {p["name"]: p for p in visits.doctor_patients(1, group="new")}
    assert set(new) == {"Mia", "Sofia"} and new["Sofia"]["waiting"] and new["Mia"]["next_visit"]
    assert [p["name"] for p in visits.doctor_patients(1, query="so", group="new")] == ["Sofia"]

    record = visits.doctor_patient(1, PATIENTS["Sofia"])  # waiting only, but still the doctor's to see
    assert record["group"] == "new" and record["waiting"] and record["history"] == []
    with pytest.raises(NotFound):
        visits.doctor_patient(2, PATIENTS["Sofia"])  # not on Dr. Brooks's list at all
    with pytest.raises(ClinicError):
        visits.doctor_patients(1, group="everyone")


def test_on_a_weekend_the_doctor_still_has_fridays_visits_to_write_up():
    from datetime import datetime

    from .test_api import as_doctor, make_client
    client, _ = make_client(now=datetime(2026, 10, 18, 20, 17))  # Sunday evening
    as_doctor(client)
    today = client.get("/api/doctor/today").json()
    assert today["visits"] == [] and today["earlier"]
    assert all(v["start"].startswith("2026-10-16") for v in today["earlier"])  # Friday
    assert today["counts"]["to_close"] == len(today["earlier"])
    visit = today["earlier"][0]
    out = client.post(f"/api/doctor/appointments/{visit['id']}/check-note", json={"note": "BP 1380/88, all fine."})
    assert out.status_code == 200
    assert client.post(f"/api/doctor/appointments/{visit['id']}/complete",
                       json={"note": "Seen. BP 128/80, continue the plan."}).status_code == 200
    assert len(client.get("/api/doctor/today").json()["earlier"]) == len(today["earlier"]) - 1


def test_shifts_cover_the_clock_including_a_night_shift_past_midnight(clinic, avail):
    from carequeue.availability import SHIFTS, Day, _covered
    assert {n: (s, e) for n, (s, e, _) in SHIFTS.items()} == {
        "morning": ("06:00", "14:00"), "day": ("09:00", "17:00"), "evening": ("14:00", "22:00"),
        "night": ("22:00", "06:00")}
    night = {0: Day(0, 22 * 60, 6 * 60)}  # Monday night shift
    mon, tue = datetime(2026, 10, 12), datetime(2026, 10, 13)

    def at(day, h, m=0):
        start = day.replace(hour=h, minute=m)
        return _covered(night, start, start + timedelta(minutes=30))
    assert at(mon, 22) and at(mon, 23, 30) and at(tue, 0) and at(tue, 5, 30)
    assert not at(mon, 21, 30) and not at(tue, 6) and not at(tue, 22)
    custom = {0: Day(0, 18 * 60, 24 * 60)}  # "18:00" to "24:00"
    assert _covered(custom, mon.replace(hour=23, minute=30), tue) and not _covered(custom, tue, tue.replace(minute=30))
    out = avail.set_hours(1, [{"weekday": 0, "shift": "night"}, {"weekday": 2, "shift": "custom", "start": "18:00",
                                                                  "end": "24:00"}])
    assert [(h["shift"], h["start"], h["end"], h["overnight"]) for h in out["hours"] if h["working"]] == [
        ("night", "22:00", "06:00", True), ("custom", "18:00", "24:00", False)]


def test_morning_and_afternoon_never_mean_the_middle_of_the_night():
    from carequeue.clinic import matches_time_of_day
    day = datetime(2026, 10, 13)
    assert [h for h in range(24) if matches_time_of_day(day.replace(hour=h), "morning")] == list(range(6, 12))
    assert [h for h in range(24) if matches_time_of_day(day.replace(hour=h), "afternoon")] == list(range(12, 18))
    assert all(matches_time_of_day(day.replace(hour=h), "any") for h in range(24))


def test_each_part_of_the_day_is_a_fixed_window_and_night_wraps_past_midnight():
    from carequeue.clinic import TIMES_OF_DAY, matches_time_of_day
    day = datetime(2026, 10, 13)
    hours = {tod: [h for h in range(24) if matches_time_of_day(day.replace(hour=h), tod)] for tod in TIMES_OF_DAY}
    assert hours["evening"] == [18, 19, 20, 21]
    assert hours["night"] == [0, 1, 2, 3, 4, 5, 22, 23]
    # Mornings, afternoons, evenings and nights cover the clock once, with no gaps or overlaps.
    assert sorted(sum((hours[t] for t in TIMES_OF_DAY if t != "any"), [])) == list(range(24))


def test_a_night_waitlist_entry_gets_a_night_opening_and_a_morning_one_does_not(clinic, avail):
    from sqlalchemy import select

    from carequeue.models import Appointment
    from carequeue.seed import ensure_slots
    with clinic.Session() as s:  # the test grid is 9-5; add the around-the-clock grid for this week
        ensure_slots(s, date(2026, 10, 12), 5, open_hour=0, close_hour=24)
        s.commit()
    avail.set_hours(1, [{"weekday": d, "shift": "night" if d == 1 else "day"} for d in range(5)])
    # Tuesday is a night shift: bookable 10 PM to midnight. Fill it, so the 11 PM cancellation is the only opening.
    eleven_pm = datetime(2026, 10, 13, 23, 0)
    night = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, eleven_pm))
    clinic.book(PATIENTS["Liam"], slot_at(clinic, 1, datetime(2026, 10, 13, 22, 0)))
    clinic.book(PATIENTS["Sofia"], slot_at(clinic, 1, datetime(2026, 10, 13, 22, 30)))
    avail.add_time_off(1, datetime(2026, 10, 13, 23, 30), datetime(2026, 10, 14))
    tuesday = {"earliest": date(2026, 10, 13), "latest": date(2026, 10, 13), "provider_id": 1}
    morning = clinic.join_waitlist(PATIENTS["Noah"], time_of_day="morning", **tuesday)
    late = clinic.join_waitlist(PATIENTS["Mia"], time_of_day="night", **tuesday)
    assert morning["status"] == late["status"] == "waiting"
    clinic.cancel(night["id"])
    clinic.run_pending(max_rounds=5)
    with clinic.Session() as s:
        offer = s.scalar(select(Appointment).where(Appointment.slot_id == slot_at(clinic, 1, eleven_pm),
                                                   Appointment.status == "held"))
    # Noah joined first, but he asked for mornings; the 11 PM opening goes to Mia, who asked for nights.
    assert offer is not None and offer.waitlist_entry_id == late["id"] != morning["id"]
