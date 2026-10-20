"""Settings, read from environment variables (and a local .env file if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    if os.getenv("CQ_SKIP_DOTENV") == "1":
        return
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    database_url: str = "sqlite:///./carequeue.db"
    redis_url: str | None = None
    rabbitmq_url: str | None = None
    nexusai_url: str | None = None  # natural-language booking goes through this gateway when set
    nexusai_api_key: str | None = None
    # How long a patient's search waits for the LLM before the keyword rules answer instead.
    nexusai_timeout_s: float = 3.0
    # Doctors click a button and wait for a note check or a summary, so they can wait longer.
    nexusai_doctor_timeout_s: float = 15.0
    hold_minutes: int = 15  # how long a freed slot is held for a waitlisted patient
    min_notice_minutes: int = 60  # don't offer slots starting sooner than this
    cache_ttl_s: int = 60
    seed_demo: bool = True
    # Times are stored as clinic-local; FHIR output needs the offset, so name the clinic's zone.
    clinic_timezone: str = "America/Los_Angeles"
    horizon_days: int = 14
    run_background_worker: bool = True  # in-process dispatcher, consumer and hold sweeper
    # The slot grid: around the clock, every day. Each doctor's shifts decide which slots are bookable,
    # so a doctor can switch to evenings or nights without new slots being created.
    clinic_open_hour: int = 0
    clinic_close_hour: int = 24
    # Test-only shortcut (POST /api/auth/demo). Off unless CQ_DEMO_LOGINS=1; the sign-in page has no demo buttons.
    demo_logins: bool = False
    # Password for the sample accounts (Dr. Patel, Harper Lopez, ...). Unset: they can't be signed in to.
    demo_password: str | None = None
    slot_minutes: int = 30
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            redis_url=os.getenv("REDIS_URL") or None,
            rabbitmq_url=os.getenv("RABBITMQ_URL") or None,
            nexusai_url=(os.getenv("NEXUSAI_URL") or "").rstrip("/") or None,
            nexusai_api_key=os.getenv("NEXUSAI_API_KEY") or None,
            nexusai_timeout_s=float(os.getenv("NEXUSAI_TIMEOUT_S", cls.nexusai_timeout_s)),
            nexusai_doctor_timeout_s=float(os.getenv("NEXUSAI_DOCTOR_TIMEOUT_S", cls.nexusai_doctor_timeout_s)),
            hold_minutes=_int("CQ_HOLD_MINUTES", cls.hold_minutes),
            min_notice_minutes=_int("CQ_MIN_NOTICE_MINUTES", cls.min_notice_minutes),
            cache_ttl_s=_int("CQ_CACHE_TTL_S", cls.cache_ttl_s),
            seed_demo=os.getenv("CQ_SEED_DEMO", "1") == "1",
            horizon_days=_int("CQ_HORIZON_DAYS", cls.horizon_days),
            run_background_worker=os.getenv("CQ_BACKGROUND_WORKER", "1") == "1",
            demo_logins=os.getenv("CQ_DEMO_LOGINS", "0") == "1",
            demo_password=os.getenv("CQ_DEMO_PASSWORD") or None,
            clinic_timezone=os.getenv("CQ_CLINIC_TIMEZONE", cls.clinic_timezone),
        )
