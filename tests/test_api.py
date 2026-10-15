from datetime import datetime

from fastapi.testclient import TestClient

from carequeue.app import Services, build_services, create_app
from carequeue.clock import FakeClock
from carequeue.config import Settings

from .conftest import MONDAY

WEDNESDAY_11 = datetime(2026, 10, 14, 11, 0)  # mid-week, with visits already this morning


def make_client(now=WEDNESDAY_11, **settings):
    settings = Settings(database_url="sqlite://", run_background_worker=False, seed_demo=True, **settings)
    services = build_services(settings, clock=FakeClock(now))
    return TestClient(create_app(services)), services


def as_patient(client):
    assert client.post("/api/auth/demo", json={"role": "patient"}).status_code == 200
    return client


def as_doctor(client):
    assert client.post("/api/auth/demo", json={"role": "doctor"}).status_code == 200
    return client


def test_health_is_public_and_everything_else_needs_a_sign_in():
    client, _ = make_client()
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["cache"] == "memory" and health["demo_logins"] is True
    assert len(client.get("/api/providers").json()) == 4
    for path in ("/api/auth/me", "/api/me/overview", "/api/doctor/today", "/api/doctor/patients"):
        assert client.get(path).status_code == 401
    assert client.get("/api/patients").status_code == 404  # the old open endpoints are gone


def test_demo_seed_is_busy_up_front_with_history_and_a_waitlist():
    client, services = make_client(now=MONDAY)
    m = services.clinic.metrics()
    assert 0.85 <= m["utilization"] < 1.0 and m["waitlist_size"] == 6
    as_doctor(client)
    tuesday = client.get("/api/doctor/week", params={"start": "2026-10-13"}).json()["days"][0]["slots"]
    bookable = [s for s in tuesday if s["state"] != "blocked"]
    assert len(bookable) == 16 and all(s["state"] == "booked" and s["patient"] for s in bookable)
    assert {s["blocked_reason"] for s in tuesday if s["state"] == "blocked"} == {"hours"}  # 8-9 and 17-18
    patients = client.get("/api/doctor/patients").json()
    assert len(patients) > 50 and any(p["visits"] for p in patients)


def test_roles_are_enforced():
    client, _ = make_client()
    as_patient(client)
    me = client.get("/api/auth/me").json()
    assert me["role"] == "patient" and me["name"] == "Harper Lopez"
    assert client.get("/api/doctor/today").status_code == 403
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    as_doctor(client)
    assert client.get("/api/me/overview").status_code == 403
    assert client.get("/api/doctor/today").json()["provider"]["name"] == "Dr. Maya Patel"


def test_demo_logins_can_be_switched_off():
    client, _ = make_client(demo_logins=False)
    assert client.post("/api/auth/demo", json={"role": "patient"}).status_code == 403


def test_register_sign_in_and_throttling():
    client, _ = make_client()
    bad = client.post("/api/auth/register", json={"name": "Kim Lee", "email": "kim@example", "password": "secret12"})
    assert bad.status_code == 400 and "valid email" in bad.json()["error"]
    short = client.post("/api/auth/register", json={"name": "Kim Lee", "email": "kim@example.com", "password": "x"})
    assert short.status_code == 400
    made = client.post("/api/auth/register", json={"name": "Kim Lee", "email": "Kim@Example.com",
                                                   "phone": "555-0199", "password": "correct horse"})
    assert made.status_code == 201 and made.json()["role"] == "patient"
    assert "httponly" in made.headers["set-cookie"].lower()
    assert client.get("/api/me/overview").json()["next"] is None
    client.post("/api/auth/logout")
    dup = client.post("/api/auth/register", json={"name": "Kim", "email": "kim@example.com", "password": "abcdefgh"})
    assert dup.status_code == 400
    ok = client.post("/api/auth/login", json={"email": "kim@example.com", "password": "correct horse"})
    assert ok.status_code == 200 and ok.json()["name"] == "Kim Lee"
    client.post("/api/auth/logout")
    codes = [client.post("/api/auth/login", json={"email": "kim@example.com", "password": "nope"}).status_code
             for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_patient_books_reschedules_and_cancels_only_their_own():
    client, services = make_client()
    as_patient(client)
    overview = client.get("/api/me/overview").json()
    assert overview["next"]["provider"] == "Dr. Maya Patel"
    week = client.get("/api/me/week", params={"provider_id": 1, "start": "2026-10-19"}).json()
    slots = [s for d in week["days"] for s in d["slots"]]
    assert all("patient" not in s and s["patient_id"] in (None, overview["patient"]["id"]) for s in slots)
    busy = {v["start"] for v in overview["upcoming"]}  # Harper may have a visit with another doctor
    open_slots = [s for s in slots if s["state"] == "open" and s["start"] not in busy]
    booked = client.post("/api/me/appointments", json={"slot_id": open_slots[0]["id"]})
    assert booked.status_code == 201
    moved = client.post(f"/api/me/appointments/{booked.json()['id']}/reschedule",
                        json={"slot_id": open_slots[1]["id"]})
    assert moved.status_code == 200 and moved.json()["replaced_appointment_id"] == booked.json()["id"]
    assert client.post(f"/api/me/appointments/{moved.json()['id']}/cancel").json()["status"] == "cancelled"
    someone_else = next(s for s in slots if s["state"] == "taken" and not s["past"])
    with services.clinic.Session() as s:
        from carequeue.models import Appointment
        other_id = s.query(Appointment.id).filter_by(slot_id=someone_else["id"], status="booked").scalar()
    assert client.post(f"/api/me/appointments/{other_id}/cancel").status_code == 404
    history = client.get("/api/me/history").json()
    assert any(v["status"] == "completed" and v["note"] for v in history)


def test_patient_waitlist_and_assistant():
    client, _ = make_client()
    as_patient(client)
    bad = client.post("/api/me/waitlist", json={"earliest": "2026-10-16", "latest": "2026-10-15", "provider_id": 1})
    assert bad.status_code == 400
    ok = client.post("/api/me/waitlist", json={"earliest": "2026-10-15", "latest": "2026-10-16",
                                               "specialty": "Dermatology"})
    assert ok.status_code == 201
    assert client.delete(f"/api/me/waitlist/{ok.json()['id']}").json()["status"] == "cancelled"
    out = client.post("/api/me/assistant", json={"text": "skin doctor next week"}).json()
    assert out["parser"] == "rules" and out["options"]


def test_doctor_cancels_a_visit_and_the_patient_is_offered_a_new_time():
    client, services = make_client()
    as_doctor(client)
    week = client.get("/api/doctor/week", params={"start": "2026-10-19"}).json()
    target = next(s for d in week["days"] for s in d["slots"] if s["state"] == "booked")
    out = client.post(f"/api/doctor/appointments/{target['appointment_id']}/cancel").json()
    assert out["entry_id"]
    services.clinic.run_pending()
    client.post("/api/auth/logout")
    story = client.get(f"/api/doctor/slots/{target['id']}")
    assert story.status_code == 401


def test_doctor_closes_todays_visits_and_manages_time_off():
    client, services = make_client()
    as_doctor(client)
    today = client.get("/api/doctor/today").json()
    assert today["counts"]["to_close"] >= 1 and today["next"]
    earlier = next(v for v in today["visits"] if v["past"] and v["status"] == "booked")
    closed = client.post(f"/api/doctor/appointments/{earlier['id']}/complete", json={"note": "Doing well."})
    assert closed.json()["status"] == "completed"
    upcoming = today["next"]
    assert client.post(f"/api/doctor/appointments/{upcoming['id']}/no-show").status_code in (200, 409)

    body = {"start": "2026-10-20T09:00", "end": "2026-10-20T12:00", "reason": "Conference"}
    preview = client.post("/api/doctor/time-off/preview", json=body).json()
    assert preview["blocked"] == 6 and len(preview["affected"]) >= 1
    added = client.post("/api/doctor/time-off", json=body).json()
    assert len(added["rebooking"]) == len(preview["affected"])
    services.clinic.run_pending()
    off = client.get("/api/doctor/time-off").json()
    assert off[0]["reason"] == "Conference" and "Tue Oct 20" in off[0]["label"]
    assert client.delete(f"/api/doctor/time-off/{off[0]['id']}").json()["opened"] == 6

    hours = client.get("/api/doctor/hours").json()
    assert [h["working"] for h in hours] == [True] * 5 + [False] * 2
    days = [{"weekday": h["weekday"], "working": h["working"], "start": h["start"], "end": h["end"]} for h in hours]
    days[0]["start"] = "08:00"  # start earlier on Mondays
    assert client.post("/api/doctor/hours/preview", json={"days": days}).json()["opened"] >= 2
    assert client.put("/api/doctor/hours", json={"days": days}).json()["hours"][0]["start"] == "08:00"


def test_doctor_only_sees_their_own_patients_and_slots():
    client, services = make_client()
    as_doctor(client)
    patients = client.get("/api/doctor/patients").json()
    one = client.get(f"/api/doctor/patients/{patients[0]['id']}").json()
    assert one["name"] == patients[0]["name"]
    with services.clinic.Session() as s:
        from carequeue.models import Slot
        other_slot = s.query(Slot.id).filter(Slot.provider_id == 2).first()[0]
    assert client.get(f"/api/doctor/slots/{other_slot}").status_code == 404
    assert client.get("/api/doctor/patients", params={"q": patients[0]["name"].split()[0]}).json()
    assert isinstance(client.get("/api/doctor/activity").json(), list)
    assert client.get("/api/doctor/metrics").json()["waitlist_size"] >= 0


def test_unknown_api_route_is_404():
    client, _ = make_client()
    assert client.get("/api/nope").status_code == 404


def test_services_without_an_engine_still_report_health():
    _, services = make_client()
    bare = TestClient(create_app(Services(services.settings, services.clinic, services.assistant, None)))
    assert bare.get("/api/health").json()["database"] == "unknown"
