"""The patient chart: allergies, conditions, medications and vital signs, plus an audit log.

Who may see a chart:
- a patient sees their own;
- a doctor sees the chart of anyone they have a relationship with: a visit (past or
  booked) or a place on their waitlist. Everyone else returns "not found", so ids
  can't be probed.

Unlike visit notes (which stay with the doctor who wrote them), the chart is shared by
the clinic: an allergy recorded by one doctor matters to all of them.

Every view and every change is written to the audit log, and patients can see who
looked at their chart. That's what health-privacy rules such as HIPAA expect: an
accounting of who accessed a record, not just who changed it.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import exists, select

from .clinic import Clinic, ClinicError, Conflict, NotFound, fmt_when
from .models import (
    Allergy,
    Appointment,
    AuditEvent,
    Condition,
    Medication,
    Patient,
    Provider,
    Slot,
    Vitals,
    WaitlistEntry,
)

# Readings outside these limits are almost certainly typing mistakes ("1380" for 138), so
# they're rejected outright. Unusual but possible values are allowed; flagging those is
# the note check's job, not the database's.
VITAL_LIMITS = {
    "systolic": (50, 260, "mmHg"),
    "diastolic": (30, 160, "mmHg"),
    "heart_rate": (20, 250, "beats per minute"),
    "temperature_c": (30, 44, "°C"),
    "weight_kg": (0.5, 350, "kg"),
    "height_cm": (30, 250, "cm"),
}
CRITICALITY = ("low", "high", "unable-to-assess")
CATEGORIES = ("food", "medication", "environment", "biologic")


def check_vitals(values: dict) -> dict[str, float]:
    """The readings that were given, as numbers, or an error if one can't be real."""
    readings = {}
    for name, value in values.items():
        if name not in VITAL_LIMITS or value in (None, ""):
            continue
        try:
            readings[name] = float(value)
        except (TypeError, ValueError):
            raise ClinicError(f"{name.replace('_', ' ')} must be a number.") from None
    for name, value in readings.items():
        low, high, unit = VITAL_LIMITS[name]
        if not low <= value <= high:
            label = name.replace("_c", "").replace("_kg", "").replace("_cm", "").replace("_", " ")
            raise ClinicError(f"A {label} of {value:g} {unit} isn't possible. Please check it.")
    if "systolic" in readings and "diastolic" in readings and readings["diastolic"] >= readings["systolic"]:
        raise ClinicError("Diastolic pressure must be lower than systolic.")
    return readings


def age_on(birth: date | None, today: date) -> int | None:
    if birth is None:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _vitals(v: Vitals) -> dict:
    return {"id": v.id, "taken_at": v.taken_at.isoformat(), "label": fmt_when(v.taken_at),
            "appointment_id": v.appointment_id, "systolic": v.systolic, "diastolic": v.diastolic,
            "heart_rate": v.heart_rate, "temperature_c": v.temperature_c, "weight_kg": v.weight_kg,
            "height_cm": v.height_cm, "bmi": v.bmi, "source": v.source}


class Chart:
    def __init__(self, clinic: Clinic):
        self.clinic = clinic
        self.Session = clinic.Session
        self.clock = clinic.clock

    # ------------------------------------------------------------------ access
    def _related(self, s, provider_id: int, patient_id: int) -> bool:
        visit = s.scalar(select(exists().where(Appointment.patient_id == patient_id,
                                               Appointment.slot_id == Slot.id, Slot.provider_id == provider_id)))
        waiting = s.scalar(select(exists().where(WaitlistEntry.patient_id == patient_id,
                                                 WaitlistEntry.provider_id == provider_id)))
        return bool(visit or waiting)

    def _check(self, s, user: dict, patient_id: int, write: bool = False) -> Patient:
        patient = s.get(Patient, patient_id)
        if patient is None:
            raise NotFound(f"patient {patient_id} not found")
        if user["role"] == "patient":
            if user["patient_id"] != patient_id or write:
                raise NotFound(f"patient {patient_id} not found")
        elif not self._related(s, user["provider_id"], patient_id):
            raise NotFound(f"patient {patient_id} not found")
        return patient

    def _audit(self, s, user: dict, action: str, patient_id: int, detail: str = "") -> None:
        s.add(AuditEvent(at=self.clock.now(), user_id=user.get("id"), actor=user["name"], role=user["role"],
                         action=action, patient_id=patient_id, detail=detail[:300]))

    # ------------------------------------------------------------------ reading
    def view(self, user: dict, patient_id: int, audit_action: str = "view_chart") -> dict:
        today = self.clock.now().date()
        with self.Session() as s:
            patient = self._check(s, user, patient_id)
            allergies = s.scalars(select(Allergy).where(Allergy.patient_id == patient_id)
                                  .order_by(Allergy.status, Allergy.substance)).all()
            conditions = s.scalars(select(Condition).where(Condition.patient_id == patient_id)
                                   .order_by(Condition.status, Condition.onset.desc())).all()
            meds = s.scalars(select(Medication).where(Medication.patient_id == patient_id)
                             .order_by(Medication.status, Medication.started.desc())).all()
            vitals = s.scalars(select(Vitals).where(Vitals.patient_id == patient_id)
                               .order_by(Vitals.taken_at.desc()).limit(12)).all()
            names = dict(s.execute(select(Provider.id, Provider.name)).all())
            if audit_action:
                self._audit(s, user, audit_action, patient_id)
            s.commit()
            return {
                "patient": {"id": patient.id, "name": patient.name, "sex": patient.sex,
                            "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
                            "age": age_on(patient.birth_date, today)},
                "allergies": [{"id": a.id, "substance": a.substance, "code": a.code, "category": a.category,
                               "criticality": a.criticality, "reaction": a.reaction, "status": a.status,
                               "recorded_on": a.recorded_on.isoformat() if a.recorded_on else None,
                               "recorded_by": names.get(a.recorded_by), "source": a.source} for a in allergies],
                "conditions": [{"id": c.id, "name": c.name, "code": c.code, "status": c.status,
                                "onset": c.onset.isoformat() if c.onset else None,
                                "resolved": c.resolved.isoformat() if c.resolved else None,
                                "recorded_by": names.get(c.recorded_by), "source": c.source} for c in conditions],
                "medications": [{"id": m.id, "name": m.name, "code": m.code, "dosage": m.dosage, "status": m.status,
                                 "started": m.started.isoformat() if m.started else None,
                                 "stopped": m.stopped.isoformat() if m.stopped else None,
                                 "prescribed_by": names.get(m.prescribed_by), "source": m.source} for m in meds],
                "vitals": [_vitals(v) for v in vitals],
            }

    def brief(self, provider_id: int, patient_id: int) -> dict:
        """What a doctor needs at a glance before a visit: active allergies, conditions and medications.

        Shown on the doctor's own "next patient" card, so it isn't logged as a separate view;
        opening the full chart is.
        """
        with self.Session() as s:
            if not self._related(s, provider_id, patient_id):
                return {"allergies": [], "conditions": [], "medications": []}
            allergies = s.scalars(select(Allergy.substance).where(Allergy.patient_id == patient_id,
                                                                  Allergy.status == "active")).all()
            conditions = s.scalars(select(Condition.name).where(Condition.patient_id == patient_id,
                                                                Condition.status == "active")).all()
            meds = s.scalars(select(Medication.name).where(Medication.patient_id == patient_id,
                                                           Medication.status == "active")).all()
        return {"allergies": sorted(allergies), "conditions": sorted(conditions), "medications": sorted(meds)}

    def access_log(self, user: dict, patient_id: int, limit: int = 30) -> list[dict]:
        """Who opened or changed this chart. Patients see their own; doctors see it for their patients."""
        with self.Session() as s:
            self._check(s, user, patient_id)
            rows = s.scalars(select(AuditEvent).where(AuditEvent.patient_id == patient_id)
                             .order_by(AuditEvent.id.desc()).limit(limit)).all()
            return [{"id": e.id, "at": e.at.isoformat(), "actor": e.actor, "role": e.role, "action": e.action,
                     "detail": e.detail} for e in rows]

    # ------------------------------------------------------------------ changes (doctors only)
    def _doctor(self, user: dict) -> int:
        if user["role"] != "doctor":
            raise ClinicError("Only doctors can change a chart.")
        return user["provider_id"]

    def add_allergy(self, user: dict, patient_id: int, substance: str, reaction: str = "",
                    criticality: str | None = None, category: str | None = None) -> dict:
        doctor = self._doctor(user)
        substance = substance.strip()
        if not substance:
            raise ClinicError("Say what the patient is allergic to.")
        if criticality and criticality not in CRITICALITY:
            raise ClinicError(f"criticality must be one of {', '.join(CRITICALITY)}")
        if category and category not in CATEGORIES:
            raise ClinicError(f"category must be one of {', '.join(CATEGORIES)}")
        with self.Session() as s:
            self._check(s, user, patient_id, write=True)
            dup = s.scalar(select(Allergy.id).where(Allergy.patient_id == patient_id, Allergy.status == "active",
                                                    Allergy.substance.ilike(substance)))
            if dup:
                raise Conflict(f"{substance} is already on the allergy list.")
            row = Allergy(patient_id=patient_id, substance=substance[:200], reaction=reaction.strip()[:200] or None,
                          criticality=criticality, category=category, status="active",
                          recorded_on=self.clock.now().date(), recorded_by=doctor, source="clinic")
            s.add(row)
            self._audit(s, user, "add_allergy", patient_id, substance)
            s.commit()
            return {"id": row.id}

    def set_allergy_status(self, user: dict, allergy_id: int, status: str) -> dict:
        self._doctor(user)
        if status not in ("active", "inactive"):
            raise ClinicError("status must be active or inactive")
        with self.Session() as s:
            row = s.get(Allergy, allergy_id)
            if row is None:
                raise NotFound(f"allergy {allergy_id} not found")
            self._check(s, user, row.patient_id, write=True)
            row.status = status
            self._audit(s, user, f"allergy_{status}", row.patient_id, row.substance)
            s.commit()
            return {"id": row.id, "status": row.status}

    def add_condition(self, user: dict, patient_id: int, name: str, onset: date | None = None) -> dict:
        doctor = self._doctor(user)
        name = name.strip()
        if not name:
            raise ClinicError("Name the condition.")
        today = self.clock.now().date()
        if onset and onset > today:
            raise ClinicError("A condition can't start in the future.")
        with self.Session() as s:
            self._check(s, user, patient_id, write=True)
            row = Condition(patient_id=patient_id, name=name[:200], status="active", onset=onset or today,
                            recorded_by=doctor, source="clinic")
            s.add(row)
            self._audit(s, user, "add_condition", patient_id, name)
            s.commit()
            return {"id": row.id}

    def resolve_condition(self, user: dict, condition_id: int) -> dict:
        self._doctor(user)
        with self.Session() as s:
            row = s.get(Condition, condition_id)
            if row is None:
                raise NotFound(f"condition {condition_id} not found")
            self._check(s, user, row.patient_id, write=True)
            if row.status == "resolved":
                raise Conflict(f"{row.name} is already resolved.")
            row.status, row.resolved = "resolved", self.clock.now().date()
            self._audit(s, user, "resolve_condition", row.patient_id, row.name)
            s.commit()
            return {"id": row.id, "status": row.status}

    def add_medication(self, user: dict, patient_id: int, name: str, dosage: str = "") -> dict:
        doctor = self._doctor(user)
        name = name.strip()
        if not name:
            raise ClinicError("Name the medication.")
        with self.Session() as s:
            self._check(s, user, patient_id, write=True)
            dup = s.scalar(select(Medication.id).where(Medication.patient_id == patient_id,
                                                       Medication.status == "active", Medication.name.ilike(name)))
            if dup:
                raise Conflict(f"{name} is already an active medication.")
            from .notecheck import allergy_conflicts  # here, to avoid a circular import

            allergies = [{"substance": a.substance, "category": a.category} for a in s.scalars(
                select(Allergy).where(Allergy.patient_id == patient_id, Allergy.status == "active"))]
            warnings = [f["message"].replace("the note mentions", "this is")
                        for f in allergy_conflicts(name, allergies)]
            row = Medication(patient_id=patient_id, name=name[:300], dosage=dosage.strip()[:200] or None,
                             status="active", started=self.clock.now().date(), prescribed_by=doctor, source="clinic")
            s.add(row)
            self._audit(s, user, "add_medication", patient_id, name + (" (allergy warning shown)" if warnings else ""))
            s.commit()
            return {"id": row.id, "warnings": warnings}

    def stop_medication(self, user: dict, medication_id: int) -> dict:
        self._doctor(user)
        with self.Session() as s:
            row = s.get(Medication, medication_id)
            if row is None:
                raise NotFound(f"medication {medication_id} not found")
            self._check(s, user, row.patient_id, write=True)
            if row.status == "stopped":
                raise Conflict(f"{row.name} is already stopped.")
            row.status, row.stopped = "stopped", self.clock.now().date()
            self._audit(s, user, "stop_medication", row.patient_id, row.name)
            s.commit()
            return {"id": row.id, "status": row.status}

    def record_vitals(self, user: dict, appointment_id: int, values: dict) -> dict | None:
        """Vitals taken at a visit. Only the fields given are stored; nothing given, nothing stored."""
        doctor = self._doctor(user)
        readings = check_vitals(values)
        if not readings:
            return None
        with self.Session() as s:
            appt = s.get(Appointment, appointment_id)
            if appt is None or appt.slot.provider_id != doctor:
                raise NotFound(f"appointment {appointment_id} not found")
            row = s.scalar(select(Vitals).where(Vitals.appointment_id == appointment_id))
            if row is None:
                row = Vitals(patient_id=appt.patient_id, appointment_id=appointment_id, taken_at=appt.slot.start,
                             recorded_by=doctor, source="clinic")
                s.add(row)
            for name, value in readings.items():
                setattr(row, name, round(value, 1))
            if row.weight_kg and row.height_cm:
                row.bmi = round(row.weight_kg / (row.height_cm / 100) ** 2, 1)
            self._audit(s, user, "record_vitals", appt.patient_id,
                        ", ".join(f"{k}={v:g}" for k, v in readings.items()))
            s.commit()
            return _vitals(row)

    def vitals_for(self, appointment_id: int) -> dict | None:
        with self.Session() as s:
            row = s.scalar(select(Vitals).where(Vitals.appointment_id == appointment_id))
            return _vitals(row) if row else None
