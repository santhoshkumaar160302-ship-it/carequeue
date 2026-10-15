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
    nexusai_timeout_s: float = 25.0
    hold_minutes: int = 15  # how long a freed slot is held for a waitlisted patient
    min_notice_minutes: int = 60  # don't offer slots starting sooner than this
    cache_ttl_s: int = 60
    seed_demo: bool = True
    horizon_days: int = 14
    run_background_worker: bool = True  # in-process dispatcher, consumer and hold sweeper
    # The slot grid. Each doctor's working hours (seeded as 9:00-17:00) decide which slots are bookable,
    # so a doctor can start earlier or finish later without new slots being created.
    clinic_open_hour: int = 8
    clinic_close_hour: int = 18
    demo_logins: bool = True  # one-click demo patient / doctor buttons on the sign-in page
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
            hold_minutes=_int("CQ_HOLD_MINUTES", cls.hold_minutes),
            min_notice_minutes=_int("CQ_MIN_NOTICE_MINUTES", cls.min_notice_minutes),
            cache_ttl_s=_int("CQ_CACHE_TTL_S", cls.cache_ttl_s),
            seed_demo=os.getenv("CQ_SEED_DEMO", "1") == "1",
            horizon_days=_int("CQ_HORIZON_DAYS", cls.horizon_days),
            run_background_worker=os.getenv("CQ_BACKGROUND_WORKER", "1") == "1",
            demo_logins=os.getenv("CQ_DEMO_LOGINS", "1") == "1",
        )
