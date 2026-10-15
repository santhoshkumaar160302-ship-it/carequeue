import threading
from datetime import datetime

import pytest

from carequeue.clinic import Conflict, NotFound

from .conftest import PATIENTS, build_clinic
from .helpers import slot_at

TUE_10 = datetime(2026, 10, 13, 10, 0)


def test_book_and_see_it_in_the_schedule(clinic):
    slot = slot_at(clinic, 1, TUE_10)
    appt = clinic.book(PATIENTS["Ava"], slot)
    assert appt["status"] == "booked" and appt["provider"] == "Dr. Maya Patel"
    day = clinic.schedule(1, TUE_10.date())
    assert next(s for s in day["slots"] if s["id"] == slot)["state"] == "booked"
    assert len(day["slots"]) == 16  # 9:00 to 17:00 in 30-minute slots


def test_a_slot_cannot_be_booked_twice(clinic):
    slot = slot_at(clinic, 1, TUE_10)
    clinic.book(PATIENTS["Ava"], slot)
    with pytest.raises(Conflict, match="Someone else"):
        clinic.book(PATIENTS["Noah"], slot)


def test_cannot_book_the_past_or_double_book_a_patient(clinic, clock):
    clock.set(datetime(2026, 10, 12, 9, 15))
    with pytest.raises(Conflict, match="already started"):
        clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, datetime(2026, 10, 12, 9, 0)))
    clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, TUE_10))
    with pytest.raises(Conflict, match="already has an appointment"):
        clinic.book(PATIENTS["Ava"], slot_at(clinic, 2, TUE_10))  # same time, other provider


def test_unknown_ids_are_not_found(clinic):
    with pytest.raises(NotFound):
        clinic.book(PATIENTS["Ava"], 99999)
    with pytest.raises(NotFound):
        clinic.cancel(12345)


def test_cancel_reopens_the_slot(clinic):
    slot = slot_at(clinic, 1, TUE_10)
    appt = clinic.book(PATIENTS["Ava"], slot)
    clinic.cancel(appt["id"])
    assert next(s for s in clinic.schedule(1, TUE_10.date())["slots"] if s["id"] == slot)["state"] == "open"
    clinic.book(PATIENTS["Noah"], slot)  # someone else can take it now
    with pytest.raises(Conflict):
        clinic.cancel(appt["id"])  # already cancelled


def test_schedule_is_cached_and_invalidated_on_change(clinic):
    first = clinic.schedule(1, TUE_10.date())
    second = clinic.schedule(1, TUE_10.date())
    assert first["source"] == "database" and second["source"] == "cache"
    clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, TUE_10))
    third = clinic.schedule(1, TUE_10.date())
    assert third["source"] == "database"  # the booking invalidated that day
    assert any(s["state"] == "booked" for s in third["slots"])


def test_past_flag_is_computed_at_read_time(clinic, clock):
    clinic.schedule(1, TUE_10.date())  # cached while every slot is in the future
    clock.set(datetime(2026, 10, 13, 12, 0))
    slots = clinic.schedule(1, TUE_10.date())["slots"]
    assert slots[0]["past"] and not slots[-1]["past"]


def test_concurrent_bookings_only_one_wins_sqlite(tmp_path):
    # A file database: an in-memory one shares a single connection, which can't run in parallel.
    c, engine = build_clinic(f"sqlite:///{tmp_path / 'race.db'}")
    try:
        _race(c)
    finally:
        engine.dispose()


def _race(clinic, n=12):
    slot = slot_at(clinic, 2, TUE_10)
    with clinic.Session() as s:
        from carequeue.models import Patient

        s.add_all(Patient(name=f"P{i}", phone="555") for i in range(n))
        s.commit()
    ids = [p["id"] for p in clinic.patients() if p["name"].startswith("P")][:n]
    results, barrier = [], threading.Barrier(n)

    def attempt(pid):
        barrier.wait()
        try:
            clinic.book(pid, slot)
            results.append("ok")
        except Conflict:
            results.append("conflict")
        except Exception as exc:  # e.g. SQLite "database is locked" also means it lost
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=attempt, args=(pid,)) for pid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1, results
    assert sum(1 for a in clinic.appointments() if a["slot_id"] == slot) == 1


@pytest.mark.skipif("not __import__('os').getenv('CQ_TEST_DATABASE_URL')")
def test_concurrent_bookings_only_one_wins_postgres():
    import os

    c, engine = build_clinic(os.environ["CQ_TEST_DATABASE_URL"])
    try:
        _race(c, n=20)
    finally:
        engine.dispose()
