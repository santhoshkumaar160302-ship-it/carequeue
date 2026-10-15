"""HTTP API (FastAPI) and the built React app.

Run locally:  uvicorn carequeue.app:create_app --factory --reload
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .assistant import Assistant
from .auth import SESSION_DAYS, Auth, AuthError, Forbidden
from .availability import Availability
from .broker import make_broker
from .cache import make_cache
from .clinic import Clinic, ClinicError, NotFound
from .clock import Clock
from .config import Settings
from .db import create_schema, make_engine, make_session_factory
from .seed import seed_demo
from .visits import Visits

log = logging.getLogger("carequeue")
FRONTEND_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "cq_session"


@dataclass
class Services:
    settings: Settings
    clinic: Clinic
    assistant: Assistant
    engine: object


def build_services(settings: Settings | None = None, clock: Clock | None = None, cache=None, broker=None,
                   transport=None) -> Services:
    settings = settings or Settings.from_env()
    clock = clock or Clock()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    Session = make_session_factory(engine)
    clinic = Clinic(Session, clock, settings, cache or make_cache(settings.redis_url, settings.cache_ttl_s),
                    broker or make_broker(settings.rabbitmq_url))
    if settings.seed_demo:
        with Session() as s:
            seed_demo(s, clock.now(), settings.horizon_days, open_hour=settings.clinic_open_hour,
                      close_hour=settings.clinic_close_hour, slot_minutes=settings.slot_minutes)
    assistant = Assistant(clinic, settings.nexusai_url, settings.nexusai_api_key, settings.nexusai_timeout_s,
                          transport=transport)
    return Services(settings, clinic, assistant, engine)


def _background_loop(clinic: Clinic, stop: threading.Event, interval_s: float = 1.0) -> None:
    """Expire holds, publish outbox events and consume the queue, about once a second."""
    while not stop.wait(interval_s):
        try:
            clinic.run_pending(max_rounds=5)
        except Exception:  # noqa: BLE001 - keep the loop alive; the next pass retries
            log.exception("background pass failed")


# ---------------------------------------------------------------- request bodies
class LoginIn(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


class RegisterIn(BaseModel):
    name: str = Field(..., max_length=100)
    email: str = Field(..., max_length=200)
    phone: str = Field("", max_length=30)
    password: str = Field(..., max_length=200)


class DemoIn(BaseModel):
    role: str = Field(..., pattern="^(patient|doctor)$")


class BookIn(BaseModel):
    slot_id: int
    source: str = Field("direct", pattern="^(direct|assistant)$")


class RescheduleIn(BaseModel):
    slot_id: int


class WaitlistIn(BaseModel):
    earliest: date
    latest: date
    provider_id: int | None = None
    specialty: str | None = None
    time_of_day: str = "any"
    replaces_appointment_id: int | None = None


class AskIn(BaseModel):
    text: str = Field(..., max_length=500)


class CloseIn(BaseModel):
    note: str | None = Field(None, max_length=2000)


class NoteIn(BaseModel):
    note: str = Field(..., max_length=2000)


class DayHours(BaseModel):
    weekday: int = Field(..., ge=0, le=6)
    working: bool = True
    start: str | None = None
    end: str | None = None


class HoursIn(BaseModel):
    days: list[DayHours]


class TimeOffIn(BaseModel):
    start: datetime
    end: datetime
    reason: str = Field("", max_length=100)

    def naive(self) -> tuple[datetime, datetime]:
        """Clinic times are local and stored without a time zone (see the README)."""
        return self.start.replace(tzinfo=None), self.end.replace(tzinfo=None)


def create_app(services: Services | None = None) -> FastAPI:
    services = services or build_services()
    clinic, settings = services.clinic, services.settings
    auth = Auth(clinic.Session, clinic.clock)
    availability, visits = Availability(clinic), Visits(clinic)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        worker = None
        if settings.run_background_worker:
            worker = threading.Thread(target=_background_loop, args=(clinic, stop), daemon=True, name="cq-worker")
            worker.start()
        yield
        stop.set()

    app = FastAPI(title="CareQueue", version=__version__, lifespan=lifespan)

    @app.exception_handler(ClinicError)
    async def clinic_error(_req: Request, exc: ClinicError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def value_error(_req: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ------------------------------------------------------------ who is asking
    def current_user(request: Request) -> dict:
        user = auth.user_for(request.cookies.get(COOKIE))
        if user is None:
            raise AuthError("Please sign in.")
        return user

    def patient(user: dict = Depends(current_user)) -> int:
        if user["role"] != "patient":
            raise Forbidden("This page is for patients.")
        return user["patient_id"]

    def doctor(user: dict = Depends(current_user)) -> int:
        if user["role"] != "doctor":
            raise Forbidden("This page is for doctors.")
        return user["provider_id"]

    def signed_in(request: Request, token: str, status: int = 200) -> JSONResponse:
        response = JSONResponse(auth.user_for(token), status_code=status)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                            secure=secure, path="/")
        return response

    # ------------------------------------------------------------ public
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "commit": os.getenv("CQ_BUILD_SHA", "dev"),
                "time": clinic.clock.now().isoformat(),
                "database": services.engine.dialect.name if services.engine else "unknown",
                "cache": clinic.cache.backend, "queue": clinic.broker.kind,
                "assistant": "nexusai" if settings.nexusai_url else "rules", "demo_logins": settings.demo_logins}

    @app.get("/api/providers")
    def providers():
        return clinic.providers()

    # ------------------------------------------------------------ sign-in
    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request):
        client = request.client.host if request.client else ""
        return signed_in(request, auth.login(body.email, body.password, client))

    @app.post("/api/auth/register", status_code=201)
    def register(body: RegisterIn, request: Request):
        return signed_in(request, auth.register(body.name, body.email, body.phone, body.password), 201)

    @app.post("/api/auth/demo")
    def demo(body: DemoIn, request: Request):
        if not settings.demo_logins:
            raise Forbidden("Demo sign-in is turned off on this server.")
        return signed_in(request, auth.demo_login(body.role))

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/auth/me")
    def me(user: dict = Depends(current_user)):
        return user

    # ------------------------------------------------------------ patient portal
    @app.get("/api/me/overview")
    def my_overview(pid: int = Depends(patient)):
        return visits.overview(pid)

    @app.get("/api/me/history")
    def my_history(pid: int = Depends(patient)):
        return visits.history(pid)

    @app.get("/api/me/week")
    def my_week(provider_id: int, start: date, pid: int = Depends(patient)):
        return visits.patient_week(pid, provider_id, start)

    @app.post("/api/me/appointments", status_code=201)
    def my_book(body: BookIn, pid: int = Depends(patient)):
        return clinic.book(pid, body.slot_id, body.source)

    @app.post("/api/me/appointments/{appointment_id}/cancel")
    def my_cancel(appointment_id: int, pid: int = Depends(patient)):
        visits.owns(pid, appointment_id)
        return clinic.cancel(appointment_id)

    @app.post("/api/me/appointments/{appointment_id}/reschedule")
    def my_reschedule(appointment_id: int, body: RescheduleIn, pid: int = Depends(patient)):
        visits.owns(pid, appointment_id)
        return clinic.reschedule(appointment_id, body.slot_id)

    @app.post("/api/me/appointments/{appointment_id}/accept")
    def my_accept(appointment_id: int, pid: int = Depends(patient)):
        visits.owns(pid, appointment_id)
        return clinic.accept_hold(appointment_id)

    @app.post("/api/me/appointments/{appointment_id}/decline")
    def my_decline(appointment_id: int, pid: int = Depends(patient)):
        visits.owns(pid, appointment_id)
        return clinic.decline_hold(appointment_id)

    @app.post("/api/me/waitlist", status_code=201)
    def my_join(body: WaitlistIn, pid: int = Depends(patient)):
        return clinic.join_waitlist(pid, **body.model_dump())

    @app.delete("/api/me/waitlist/{entry_id}")
    def my_leave(entry_id: int, pid: int = Depends(patient)):
        visits.owns_entry(pid, entry_id)
        return clinic.leave_waitlist(entry_id)

    @app.post("/api/me/assistant")
    def ask(body: AskIn, _pid: int = Depends(patient)):
        return services.assistant.suggest(body.text)

    # ------------------------------------------------------------ doctor portal
    @app.get("/api/doctor/today")
    def doctor_today(prov: int = Depends(doctor)):
        return visits.today(prov)

    @app.get("/api/doctor/week")
    def doctor_week(start: date, prov: int = Depends(doctor)):
        return visits.doctor_week(prov, start)

    @app.get("/api/doctor/slots/{slot_id}")
    def doctor_slot(slot_id: int, prov: int = Depends(doctor)):
        story = clinic.slot_story(slot_id)
        if story["slot"]["provider_id"] != prov:
            raise NotFound(f"slot {slot_id} not found")
        return story

    @app.post("/api/doctor/appointments/{appointment_id}/complete")
    def doctor_complete(appointment_id: int, body: CloseIn, prov: int = Depends(doctor)):
        return visits.close(prov, appointment_id, "completed", body.note)

    @app.post("/api/doctor/appointments/{appointment_id}/no-show")
    def doctor_no_show(appointment_id: int, prov: int = Depends(doctor)):
        return visits.close(prov, appointment_id, "no_show")

    @app.put("/api/doctor/appointments/{appointment_id}/note")
    def doctor_note(appointment_id: int, body: NoteIn, prov: int = Depends(doctor)):
        return visits.set_note(prov, appointment_id, body.note)

    @app.post("/api/doctor/appointments/{appointment_id}/cancel")
    def doctor_cancel(appointment_id: int, prov: int = Depends(doctor)):
        visits.own_check(prov, appointment_id)
        return availability.displace(appointment_id, "doctor")

    @app.get("/api/doctor/hours")
    def get_hours(prov: int = Depends(doctor)):
        return availability.hours(prov)

    @app.post("/api/doctor/hours/preview")
    def preview_hours(body: HoursIn, prov: int = Depends(doctor)):
        return availability.preview_hours(prov, [d.model_dump() for d in body.days])

    @app.put("/api/doctor/hours")
    def put_hours(body: HoursIn, prov: int = Depends(doctor)):
        return availability.set_hours(prov, [d.model_dump() for d in body.days])

    @app.get("/api/doctor/time-off")
    def get_time_off(prov: int = Depends(doctor)):
        return availability.time_off(prov)

    @app.post("/api/doctor/time-off/preview")
    def preview_time_off(body: TimeOffIn, prov: int = Depends(doctor)):
        return availability.preview_time_off(prov, *body.naive())

    @app.post("/api/doctor/time-off", status_code=201)
    def add_time_off(body: TimeOffIn, prov: int = Depends(doctor)):
        return availability.add_time_off(prov, *body.naive(), body.reason)

    @app.delete("/api/doctor/time-off/{time_off_id}")
    def remove_time_off(time_off_id: int, prov: int = Depends(doctor)):
        return availability.remove_time_off(prov, time_off_id)

    @app.get("/api/doctor/patients")
    def doctor_patients(q: str = "", prov: int = Depends(doctor)):
        return visits.doctor_patients(prov, q)

    @app.get("/api/doctor/patients/{patient_id}")
    def doctor_patient(patient_id: int, prov: int = Depends(doctor)):
        return visits.doctor_patient(prov, patient_id)

    @app.get("/api/doctor/activity")
    def doctor_activity(prov: int = Depends(doctor)):
        return clinic.activity(provider_id=prov)

    @app.get("/api/doctor/waitlist")
    def doctor_waitlist(prov: int = Depends(doctor)):
        name = next(p["name"] for p in clinic.providers() if p["id"] == prov)
        return [w for w in clinic.waitlist() if w["provider"] == name]

    @app.get("/api/doctor/metrics")
    def doctor_metrics(_prov: int = Depends(doctor)):
        return clinic.metrics()

    if FRONTEND_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            target = FRONTEND_DIR / path
            if path and target.is_file() and FRONTEND_DIR in target.resolve().parents:
                return FileResponse(target)
            return FileResponse(FRONTEND_DIR / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return {"message": "CareQueue API is running. Build the frontend (see README) or open /docs."}

    return app
