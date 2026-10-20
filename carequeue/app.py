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

from . import __version__, notecheck, summary
from .assistant import Assistant
from .auth import SESSION_DAYS, Auth, AuthError, Forbidden
from .availability import Availability
from .broker import make_broker
from .cache import make_cache
from .chart import Chart, check_vitals
from .clinic import Clinic, ClinicError, NotFound
from .clock import Clock
from .config import Settings
from .db import create_schema, make_engine, make_session_factory
from .fhir import SEARCHABLE, Fhir
from .llm import Gateway
from .models import Appointment
from .seed import seed_demo
from .visits import Visits

log = logging.getLogger("carequeue")
FRONTEND_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "cq_session"
FHIR_JSON = "application/fhir+json"


@dataclass
class Services:
    settings: Settings
    clinic: Clinic
    assistant: Assistant
    engine: object
    gateway: Gateway | None = None  # NexusAI, for the note check and summaries


def build_services(settings: Settings | None = None, clock: Clock | None = None, cache=None, broker=None,
                   transport=None) -> Services:
    settings = settings or Settings.from_env()
    clock = clock or Clock(settings.clinic_timezone)
    engine = make_engine(settings.database_url)
    create_schema(engine)
    Session = make_session_factory(engine)
    clinic = Clinic(Session, clock, settings, cache or make_cache(settings.redis_url, settings.cache_ttl_s),
                    broker or make_broker(settings.rabbitmq_url))
    if settings.seed_demo:
        with Session() as s:
            seed_demo(s, clock.now(), settings.horizon_days, open_hour=settings.clinic_open_hour,
                      close_hour=settings.clinic_close_hour, slot_minutes=settings.slot_minutes,
                      demo_password=settings.demo_password)
    assistant = Assistant(clinic, settings.nexusai_url, settings.nexusai_api_key, settings.nexusai_timeout_s,
                          transport=transport)
    gateway = Gateway(settings.nexusai_url, settings.nexusai_api_key, transport=transport)
    return Services(settings, clinic, assistant, engine, gateway)


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
    role: str | None = Field(None, pattern="^(patient|doctor)$")  # the tile picked on the sign-in page


class RegisterIn(BaseModel):
    name: str = Field(..., max_length=100)
    email: str = Field(..., max_length=200)
    phone: str = Field("", max_length=30)
    password: str = Field(..., max_length=200)
    birth_date: date | None = None
    sex: str | None = Field(None, pattern="^(female|male|other|unknown)$")


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


class VitalsIn(BaseModel):
    systolic: float | None = None
    diastolic: float | None = None
    heart_rate: float | None = None
    temperature_c: float | None = None
    weight_kg: float | None = None
    height_cm: float | None = None


class CloseIn(BaseModel):
    note: str | None = Field(None, max_length=2000)
    vitals: VitalsIn | None = None


class NoteCheckIn(BaseModel):
    note: str = Field("", max_length=2000)
    vitals: VitalsIn | None = None


class AllergyIn(BaseModel):
    substance: str = Field(..., max_length=200)
    reaction: str = Field("", max_length=200)
    criticality: str | None = None
    category: str | None = None


class StatusIn(BaseModel):
    status: str


class ConditionIn(BaseModel):
    name: str = Field(..., max_length=200)
    onset: date | None = None


class MedicationIn(BaseModel):
    name: str = Field(..., max_length=300)
    dosage: str = Field("", max_length=200)


class NoteIn(BaseModel):
    note: str = Field(..., max_length=2000)


class DayHours(BaseModel):
    weekday: int = Field(..., ge=0, le=6)
    working: bool = True
    start: str | None = None
    end: str | None = None
    shift: str | None = None  # morning | day | evening | night | custom (custom uses start and end)


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
    chart = Chart(clinic)
    gateway = services.gateway or Gateway(settings.nexusai_url, settings.nexusai_api_key)
    fhir = Fhir(chart, settings.clinic_timezone)
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
    async def clinic_error(req: Request, exc: ClinicError):
        if req.url.path.startswith("/fhir/"):  # FHIR clients expect an OperationOutcome
            code = {401: "login", 403: "forbidden", 404: "not-found", 409: "conflict"}.get(exc.status, "invalid")
            return JSONResponse({"resourceType": "OperationOutcome",
                                 "issue": [{"severity": "error", "code": code, "diagnostics": str(exc)}]},
                                status_code=exc.status, media_type=FHIR_JSON)
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
                "assistant": "nexusai" if settings.nexusai_url else "rules", "demo_logins": settings.demo_logins,
                "timezone": settings.clinic_timezone}

    @app.get("/api/providers")
    def providers():
        return clinic.providers()

    # ------------------------------------------------------------ sign-in
    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request):
        client = request.client.host if request.client else ""
        return signed_in(request, auth.login(body.email, body.password, client, body.role))

    @app.post("/api/auth/register", status_code=201)
    def register(body: RegisterIn, request: Request):
        return signed_in(request, auth.register(body.name, body.email, body.phone, body.password, body.birth_date,
                                                body.sex), 201)

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

    @app.get("/api/me/chart")
    def my_chart(user: dict = Depends(current_user), pid: int = Depends(patient)):
        return chart.view(user, pid)

    @app.get("/api/me/access-log")
    def my_access_log(user: dict = Depends(current_user), pid: int = Depends(patient)):
        return chart.access_log(user, pid)

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
        today = visits.today(prov)
        if today["next"]:
            today["next"]["chart"] = chart.brief(prov, today["next"]["patient_id"])
        return today

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
    def doctor_complete(appointment_id: int, body: CloseIn, user: dict = Depends(current_user),
                        prov: int = Depends(doctor)):
        readings = check_vitals(body.vitals.model_dump()) if body.vitals else {}  # check before closing
        closed = visits.close(prov, appointment_id, "completed", body.note)
        if readings:
            closed["vitals"] = chart.record_vitals(user, appointment_id, readings)
        return closed

    @app.put("/api/doctor/appointments/{appointment_id}/vitals")
    def doctor_vitals(appointment_id: int, body: VitalsIn, user: dict = Depends(current_user),
                      _prov: int = Depends(doctor)):
        return chart.record_vitals(user, appointment_id, body.model_dump())

    # ------------------------------------------------------------ AI help for doctors (via NexusAI)
    @app.post("/api/doctor/appointments/{appointment_id}/check-note")
    def check_note(appointment_id: int, body: NoteCheckIn, user: dict = Depends(current_user),
                   prov: int = Depends(doctor)):
        entered = check_vitals(body.vitals.model_dump()) if body.vitals else {}
        with clinic.Session() as s:
            ctx = notecheck.context_for(s, prov, appointment_id, entered)
            patient_id = s.get(Appointment, appointment_id).patient_id
            chart._audit(s, user, "check_note", patient_id)
            s.commit()
        return notecheck.check(body.note, ctx, gateway, settings.nexusai_doctor_timeout_s)

    @app.post("/api/doctor/patients/{patient_id}/summary")
    def patient_summary(patient_id: int, user: dict = Depends(current_user), prov: int = Depends(doctor)):
        with clinic.Session() as s:
            chart._check(s, user, patient_id)
            sources, open_items = summary.gather(s, prov, patient_id, clinic.clock.now())
            chart._audit(s, user, "summary", patient_id)
            s.commit()
        return summary.summarize(sources, open_items, gateway, settings.nexusai_doctor_timeout_s)

    # ------------------------------------------------------------ the patient chart
    @app.get("/api/doctor/patients/{patient_id}/chart")
    def doctor_chart(patient_id: int, user: dict = Depends(current_user), _prov: int = Depends(doctor)):
        return chart.view(user, patient_id)

    @app.get("/api/doctor/patients/{patient_id}/access-log")
    def doctor_access_log(patient_id: int, user: dict = Depends(current_user), _prov: int = Depends(doctor)):
        return chart.access_log(user, patient_id)

    @app.post("/api/doctor/patients/{patient_id}/allergies", status_code=201)
    def add_allergy(patient_id: int, body: AllergyIn, user: dict = Depends(current_user),
                    _prov: int = Depends(doctor)):
        return chart.add_allergy(user, patient_id, body.substance, body.reaction, body.criticality, body.category)

    @app.post("/api/doctor/allergies/{allergy_id}/status")
    def allergy_status(allergy_id: int, body: StatusIn, user: dict = Depends(current_user),
                       _prov: int = Depends(doctor)):
        return chart.set_allergy_status(user, allergy_id, body.status)

    @app.post("/api/doctor/patients/{patient_id}/conditions", status_code=201)
    def add_condition(patient_id: int, body: ConditionIn, user: dict = Depends(current_user),
                      _prov: int = Depends(doctor)):
        return chart.add_condition(user, patient_id, body.name, body.onset)

    @app.post("/api/doctor/conditions/{condition_id}/resolve")
    def resolve_condition(condition_id: int, user: dict = Depends(current_user), _prov: int = Depends(doctor)):
        return chart.resolve_condition(user, condition_id)

    @app.post("/api/doctor/patients/{patient_id}/medications", status_code=201)
    def add_medication(patient_id: int, body: MedicationIn, user: dict = Depends(current_user),
                       _prov: int = Depends(doctor)):
        return chart.add_medication(user, patient_id, body.name, body.dosage)

    @app.post("/api/doctor/medications/{medication_id}/stop")
    def stop_medication(medication_id: int, user: dict = Depends(current_user), _prov: int = Depends(doctor)):
        return chart.stop_medication(user, medication_id)

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

    @app.get("/api/doctor/shifts")
    def get_shifts(prov: int = Depends(doctor)):
        return availability.shifts()

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
    def doctor_patients(q: str = "", group: str = "all", prov: int = Depends(doctor)):
        return visits.doctor_patients(prov, q, group)

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

    # ------------------------------------------------------------ FHIR (read-only)
    def fhir_base(request: Request) -> str:
        return str(request.base_url).rstrip("/") + "/fhir"

    def fhir_json(body: dict) -> JSONResponse:
        return JSONResponse(body, media_type=FHIR_JSON)

    def patient_param(value: str) -> int:
        try:
            return int(value.removeprefix("Patient/"))
        except ValueError:
            raise NotFound(f"patient {value} not found") from None

    @app.get("/fhir/metadata", include_in_schema=False)
    def fhir_metadata(request: Request):
        return fhir_json(Fhir.capability(fhir_base(request)))

    @app.get("/fhir/Patient/{patient_id}/$everything", include_in_schema=False)
    def fhir_everything(patient_id: int, request: Request, user: dict = Depends(current_user)):
        return fhir_json(fhir.everything(user, patient_id, fhir_base(request)))

    @app.get("/fhir/Patient/{patient_id}", include_in_schema=False)
    def fhir_patient(patient_id: int, user: dict = Depends(current_user)):
        return fhir_json(fhir.patient(user, patient_id))

    @app.get("/fhir/{resource_type}", include_in_schema=False)
    def fhir_search(resource_type: str, request: Request, patient: str = "", user: dict = Depends(current_user)):
        if resource_type not in SEARCHABLE:
            raise NotFound(f"{resource_type} isn't supported; see /fhir/metadata")
        if not patient:
            raise ClinicError("Search by patient, for example ?patient=11")
        return fhir_json(fhir.search(user, resource_type, patient_param(patient), fhir_base(request)))

    if FRONTEND_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "fhir/")):
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
