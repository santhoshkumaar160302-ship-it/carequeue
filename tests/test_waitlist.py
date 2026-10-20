from datetime import date, datetime

import pytest

from carequeue.clinic import ClinicError, Conflict
from carequeue.models import OutboxEvent

from .conftest import PATIENTS
from .helpers import appointment, held_for, slot_at

TUE = date(2026, 10, 13)
TUE_10 = datetime(2026, 10, 13, 10, 0)
TUE_14 = datetime(2026, 10, 13, 14, 0)
FRI_10 = datetime(2026, 10, 16, 10, 0)


def fill_day(clinic, provider_id, day, patient):
    """Book every slot of a day so nothing is open (that's when people join the waitlist)."""
    ids = []
    for s in clinic.schedule(provider_id, day)["slots"]:
        if s["state"] == "open":
            ids.append(clinic.book(patient, s["id"])["id"])
    return ids


@pytest.fixture
def full_tuesday(clinic):
    """Tuesday with Dr. Patel is fully booked by Liam."""
    ids = fill_day(clinic, 1, TUE, PATIENTS["Liam"])
    clinic.run_pending()
    return ids


def cancel_at(clinic, when, provider_id=1):
    slot = slot_at(clinic, provider_id, when)
    appt = next(a for a in clinic.appointments() if a["slot_id"] == slot)
    clinic.cancel(appt["id"])
    clinic.run_pending()
    return slot


def test_cancellation_is_offered_to_first_in_line(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Noah"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    [hold] = held_for(clinic, PATIENTS["Ava"])
    assert hold["start"] == TUE_10.isoformat() and not held_for(clinic, PATIENTS["Noah"])
    assert [w["status"] for w in clinic.waitlist()] == ["offered", "waiting"]


def test_accepting_an_offer_books_it(clinic, full_tuesday):
    entry = clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    hold = held_for(clinic, PATIENTS["Ava"])[0]
    assert clinic.accept_hold(hold["id"])["status"] == "booked"
    assert appointment(clinic, hold["id"]).status == "booked"
    assert all(w["id"] != entry["id"] for w in clinic.waitlist())  # off the waitlist
    with pytest.raises(Conflict):
        clinic.accept_hold(hold["id"])


def test_declined_offer_goes_to_the_next_patient(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Noah"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    clinic.decline_hold(held_for(clinic, PATIENTS["Ava"])[0]["id"])
    clinic.run_pending()
    assert held_for(clinic, PATIENTS["Noah"])[0]["start"] == TUE_10.isoformat()
    # Ava stays on the list for other slots, but isn't offered the one she declined again.
    assert next(w for w in clinic.waitlist() if w["patient_id"] == PATIENTS["Ava"])["status"] == "waiting"


def test_unanswered_offer_expires_and_moves_on(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Noah"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    ava_hold = held_for(clinic, PATIENTS["Ava"])[0]
    clock.advance(minutes=14)
    clinic.run_pending()
    assert held_for(clinic, PATIENTS["Ava"])  # still within 15 minutes
    clock.advance(minutes=2)
    clinic.run_pending()
    assert appointment(clinic, ava_hold["id"]).status == "expired"
    assert held_for(clinic, PATIENTS["Noah"])
    with pytest.raises(Conflict):
        clinic.accept_hold(ava_hold["id"])


def test_nobody_waiting_leaves_the_slot_open(clinic, full_tuesday):
    slot = cancel_at(clinic, TUE_10)
    state = next(s for s in clinic.schedule(1, TUE)["slots"] if s["id"] == slot)["state"]
    assert state == "open"
    assert clinic.activity()[0]["result"] == "nobody on the waitlist matches"


def test_matching_respects_provider_specialty_and_time_of_day(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=2)  # wrong provider
    clinic.join_waitlist(PATIENTS["Noah"], TUE, TUE, specialty="Dermatology")  # wrong specialty
    clinic.join_waitlist(PATIENTS["Mia"], TUE, TUE, provider_id=1, time_of_day="afternoon")  # wrong time
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Sofia"], TUE, TUE, specialty="general practice")  # case-insensitive match
    # Ava, Noah and Mia were each offered an open slot that fits them when they joined; clear those.
    for name in ("Ava", "Noah", "Mia"):
        for h in held_for(clinic, PATIENTS[name]):
            clinic.decline_hold(h["id"])
    clinic.run_pending()
    cancel_at(clinic, TUE_10)
    assert held_for(clinic, PATIENTS["Sofia"])[0]["start"] == TUE_10.isoformat()


def test_joining_when_a_slot_is_already_open_offers_it_immediately(clinic):
    result = clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1, time_of_day="afternoon")
    assert result["status"] == "offered"
    hold = appointment(clinic, result["offered_appointment_id"])
    assert hold.status == "held"


def test_slots_too_close_to_now_are_not_offered(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    clock.set(datetime(2026, 10, 13, 9, 30))
    cancel_at(clinic, TUE_10)  # starts in 30 minutes; minimum notice is 60
    assert not held_for(clinic, PATIENTS["Ava"])
    assert clinic.activity()[0]["result"] == "too soon to offer"


def test_moving_up_releases_the_later_appointment_for_the_next_person(clinic, full_tuesday, clock):
    later = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, FRI_10))
    clinic.join_waitlist(PATIENTS["Ava"], TUE, date(2026, 10, 16), provider_id=1,
                         replaces_appointment_id=later["id"])
    for h in held_for(clinic, PATIENTS["Ava"]):  # she was offered Wednesday at once; decline to test Tuesday
        clinic.decline_hold(h["id"])
    clinic.run_pending()
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Noah"], date(2026, 10, 16), date(2026, 10, 16), provider_id=1,
                         time_of_day="morning")
    for h in held_for(clinic, PATIENTS["Noah"]):
        clinic.decline_hold(h["id"])
    clinic.run_pending()
    cancel_at(clinic, TUE_10)
    hold = held_for(clinic, PATIENTS["Ava"])[0]
    assert clinic.accept_hold(hold["id"])["released_appointment_id"] == later["id"]
    clinic.run_pending()
    assert appointment(clinic, later["id"]).status == "cancelled"
    assert held_for(clinic, PATIENTS["Noah"])[0]["start"] == FRI_10.isoformat()  # the cascade


def test_a_later_slot_is_never_offered_as_an_improvement(clinic, full_tuesday):
    nine_thirty = datetime(2026, 10, 13, 9, 30)
    cancel_at(clinic, nine_thirty)  # nobody waiting yet, so it just reopens
    current = clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, nine_thirty))
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1, replaces_appointment_id=current["id"])
    cancel_at(clinic, TUE_14)
    assert not held_for(clinic, PATIENTS["Ava"])


def test_event_handling_is_idempotent(clinic, full_tuesday):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    slot = slot_at(clinic, 1, TUE_10)
    appt = next(a for a in clinic.appointments() if a["slot_id"] == slot)
    clinic.cancel(appt["id"])
    clinic.publish_pending()
    with clinic.Session() as s:
        freed = s.query(OutboxEvent).filter_by(type="slot_freed").order_by(OutboxEvent.id.desc()).first().id
    clinic.handle_event(freed)
    clinic.handle_event(freed)  # delivered twice by the queue
    assert clinic.handle_slot_freed(slot) == "already filled"
    assert len(held_for(clinic, PATIENTS["Ava"])) == 1


def test_every_event_is_published_and_processed(clinic, full_tuesday):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    with clinic.Session() as s:
        events = s.query(OutboxEvent).all()
    assert events and all(e.published_at and e.processed_at for e in events)
    assert all(n["sent"] for n in clinic.notifications())
    kinds = {n["kind"] for n in clinic.notifications(PATIENTS["Ava"])}
    assert "offer" in kinds


def test_waitlist_validation(clinic):
    with pytest.raises(ClinicError, match="provider or a specialty"):
        clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE)
    with pytest.raises(ClinicError, match="on or after"):
        clinic.join_waitlist(PATIENTS["Ava"], TUE, date(2026, 10, 12), provider_id=1)
    with pytest.raises(ClinicError, match="time_of_day"):
        clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1, time_of_day="lunchtime")
    entry = clinic.join_waitlist(PATIENTS["Noah"], date(2026, 10, 16), date(2026, 10, 16), provider_id=2)
    clinic.leave_waitlist(entry["id"])
    with pytest.raises(Conflict):
        clinic.leave_waitlist(entry["id"])


def test_metrics_count_offers_and_utilization(clinic, full_tuesday):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    cancel_at(clinic, TUE_10)
    clinic.accept_hold(held_for(clinic, PATIENTS["Ava"])[0]["id"])
    m = clinic.metrics()
    assert m["offers"]["made"] == 1 and m["offers"]["accepted"] == 1 and m["acceptance_rate"] == 1.0
    assert m["booked"] == 16 and m["slots"] == 160 and m["cancellations"] == 1


def test_slot_story_tells_what_happened_and_who_is_next(clinic, full_tuesday, clock):
    clinic.join_waitlist(PATIENTS["Ava"], TUE, TUE, provider_id=1)
    clock.advance(minutes=1)
    clinic.join_waitlist(PATIENTS["Noah"], TUE, TUE, provider_id=1)
    slot = cancel_at(clinic, TUE_10)
    story = clinic.slot_story(slot)
    assert story["state"] == "held" and story["appointment"]["patient"] == "Ava"
    assert [e["type"] for e in story["events"]][-2:] == ["slot_freed", "hold_offered"]
    assert story["events"][-2]["cause"] == "cancelled"
    assert [n["patient"] for n in story["next_in_line"]] == ["Noah"]  # Ava holds it; Noah is next
    assert story["slot"]["provider"] == "Dr. Maya Patel" and not story["slot"]["past"]


def test_week_skips_weekends_and_carries_appointment_ids(clinic):
    week = clinic.week(1, date(2026, 10, 12))
    assert [d["date"] for d in week["days"]] == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    booked = clinic.book(PATIENTS["Ava"], week["days"][1]["slots"][0]["id"])
    again = clinic.week(1, date(2026, 10, 12))
    assert again["days"][1]["slots"][0]["appointment_id"] == booked["id"]
