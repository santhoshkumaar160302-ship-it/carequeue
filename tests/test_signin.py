"""The sign-in page's rules: patients sign up themselves, doctors are created by management,
the sample accounts use CQ_DEMO_PASSWORD, and the demo shortcut is off by default."""
from datetime import datetime

from fastapi.testclient import TestClient

from carequeue.app import build_services, create_app
from carequeue.clock import FakeClock
from carequeue.config import Settings

NOW = datetime(2026, 10, 19, 13, 0)


def client(**settings) -> TestClient:
    s = Settings(database_url="sqlite://", run_background_worker=False, seed_demo=True, **settings)
    c = TestClient(create_app(build_services(s, clock=FakeClock(NOW))))
    return c


def login(c, email, password, role=None):
    return c.post("/api/auth/login", json={"email": email, "password": password, "role": role})


def test_sample_accounts_use_the_configured_password_and_the_role_tile():
    c = client(demo_password="clinic-demo-2026")
    assert login(c, "maya.patel@demo.carequeue", "clinic-demo-2026", "doctor").json()["role"] == "doctor"
    assert login(c, "harper.lopez@demo.carequeue", "clinic-demo-2026", "patient").json()["name"] == "Harper Lopez"
    # Right password, wrong tile: a clear hint, no session.
    wrong = login(c, "maya.patel@demo.carequeue", "clinic-demo-2026", "patient")
    assert wrong.status_code == 401 and "doctor account" in wrong.json()["error"]
    assert login(c, "maya.patel@demo.carequeue", "wrong-password", "doctor").status_code == 401


def test_without_a_password_set_nobody_can_sign_in_to_the_sample_accounts():
    c = client()
    assert login(c, "maya.patel@demo.carequeue", "", "doctor").status_code == 401
    assert login(c, "maya.patel@demo.carequeue", "None", "doctor").status_code == 401


def test_the_demo_shortcut_is_off_by_default():
    c = client(demo_password="x" * 10)
    assert c.get("/api/health").json()["demo_logins"] is False
    assert c.post("/api/auth/demo", json={"role": "doctor"}).status_code == 403
    assert Settings.from_env().demo_logins is False


def test_a_patient_signs_up_with_birth_date_and_sex_and_it_reaches_their_chart():
    c = client()
    out = c.post("/api/auth/register", json={"name": "Ana Silva", "email": "ana@example.com", "phone": "555-1234",
                                             "password": "long-enough", "birth_date": "1990-04-02", "sex": "female"})
    assert out.status_code == 201 and out.json()["role"] == "patient"
    chart = c.get("/api/me/chart").json()["patient"]
    assert chart["birth_date"] == "1990-04-02" and chart["sex"] == "female" and chart["age"] == 36


def test_sign_up_checks():
    c = client()
    base = {"name": "Ana Silva", "email": "ana@example.com", "password": "long-enough"}
    assert c.post("/api/auth/register", json={**base, "birth_date": "2027-01-01"}).status_code == 400  # future
    assert c.post("/api/auth/register", json={**base, "birth_date": "1880-01-01"}).status_code == 400
    assert c.post("/api/auth/register", json={**base, "sex": "robot"}).status_code == 422
    assert c.post("/api/auth/register", json={**base, "email": "me@demo.carequeue"}).status_code == 400
    # There is no way to create a doctor account from the website.
    out = c.post("/api/auth/register", json={**base, "role": "doctor"})
    assert out.status_code == 201 and out.json()["role"] == "patient"


def test_changing_the_password_setting_applies_on_the_next_start(tmp_path):
    url = f"sqlite:///{tmp_path}/cq.db"
    first = Settings(database_url=url, run_background_worker=False, seed_demo=True, demo_password="first-pass-1")
    c1 = TestClient(create_app(build_services(first, clock=FakeClock(NOW))))
    assert login(c1, "maya.patel@demo.carequeue", "first-pass-1", "doctor").status_code == 200
    second = Settings(database_url=url, run_background_worker=False, seed_demo=True, demo_password="second-pass-2")
    c2 = TestClient(create_app(build_services(second, clock=FakeClock(NOW))))
    assert login(c2, "maya.patel@demo.carequeue", "second-pass-2", "doctor").status_code == 200
    assert login(c2, "maya.patel@demo.carequeue", "first-pass-1", "doctor").status_code == 401


def test_the_clock_reads_the_clinics_time_zone_not_the_servers():
    from datetime import UTC, timedelta

    from carequeue.clock import Clock
    utc = datetime.now(UTC).replace(tzinfo=None)
    la = Clock("America/Los_Angeles").now()
    # Los Angeles is 7 or 8 hours behind UTC, whatever zone this machine is set to.
    assert timedelta(hours=6, minutes=59) <= utc - la <= timedelta(hours=8, minutes=1)
