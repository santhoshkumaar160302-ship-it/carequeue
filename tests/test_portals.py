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
                                       "end": "12:00"}


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
    with pytest.raises(ClinicError, match="after the start"):
        avail.set_hours(1, [{"weekday": 0, "start": "12:00", "end": "09:00"}])
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
