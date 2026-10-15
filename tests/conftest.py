import os
from datetime import datetime

import pytest

os.environ["CQ_SKIP_DOTENV"] = "1"

from carequeue.broker import InMemoryBroker  # noqa: E402
from carequeue.cache import MemoryScheduleCache  # noqa: E402
from carequeue.clinic import Clinic  # noqa: E402
from carequeue.clock import FakeClock  # noqa: E402
from carequeue.config import Settings  # noqa: E402
from carequeue.db import create_schema, make_engine, make_session_factory  # noqa: E402
from carequeue.models import Base, Patient, Provider  # noqa: E402
from carequeue.seed import ensure_slots  # noqa: E402

MONDAY = datetime(2026, 10, 12, 8, 0)  # a Monday, before the clinic opens


def build_clinic(url="sqlite://", start=MONDAY):
    clock = FakeClock(start)
    settings = Settings(database_url=url, seed_demo=False, run_background_worker=False,
                        hold_minutes=15, min_notice_minutes=60)
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    create_schema(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        s.add_all([Provider(name="Dr. Maya Patel", specialty="General Practice"),
                   Provider(name="Dr. Lena Brooks", specialty="Dermatology")])
        s.add_all([Patient(name=n, phone="555-0100") for n in ("Ava", "Noah", "Mia", "Liam", "Sofia")])
        s.flush()
        ensure_slots(s, start.date(), 5)  # Mon-Fri, 9:00-17:00, 30-minute slots
        s.commit()
    return Clinic(Session, clock, settings, MemoryScheduleCache(60), InMemoryBroker()), engine


# Every clinic test runs on SQLite, and on PostgreSQL too when CQ_TEST_DATABASE_URL is set (CI does this).
BACKENDS = ["sqlite://"] + ([os.environ["CQ_TEST_DATABASE_URL"]] if os.getenv("CQ_TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS, ids=lambda u: u.split(":")[0])
def clinic(request):
    c, engine = build_clinic(request.param)
    yield c
    engine.dispose()


@pytest.fixture
def clock(clinic):
    return clinic.clock


PATIENTS = {"Ava": 1, "Noah": 2, "Mia": 3, "Liam": 4, "Sofia": 5}
