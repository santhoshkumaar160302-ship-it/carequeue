"""A small, read-only FHIR R4 API over the patient chart.

FHIR is the standard health systems use to exchange records. Serving the chart as FHIR
means another system (an EHR, a patient app, an analytics pipeline) can read CareQueue
data without a custom integration:

  GET /fhir/metadata                              what this server supports (public)
  GET /fhir/Patient/{id}                          the patient
  GET /fhir/Patient/{id}/$everything              everything below, in one Bundle
  GET /fhir/AllergyIntolerance?patient={id}       allergies          (SNOMED CT)
  GET /fhir/Condition?patient={id}                problem list       (SNOMED CT)
  GET /fhir/MedicationStatement?patient={id}      medications        (RxNorm)
  GET /fhir/Observation?patient={id}              vital signs        (LOINC, UCUM units)
  GET /fhir/Encounter?patient={id}                visits

The same access rules as the app apply (a patient reads their own record, a doctor reads
their patients'), every read is written to the audit log, and doctors only see their own
encounters. The tests validate the output against the FHIR R4B models in `fhir.resources`.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .chart import Chart
from .models import Allergy, Appointment, Condition, Medication, Patient, Provider, Slot, Vitals

SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
ALLERGY_VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
VITAL_SIGNS = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                           "code": "vital-signs", "display": "Vital Signs"}]}
# (field, LOINC code, display, unit) for single-value vital signs; blood pressure is a panel.
SIMPLE_VITALS = [
    ("heart_rate", "8867-4", "Heart rate", "/min"),
    ("temperature_c", "8310-5", "Body temperature", "Cel"),
    ("weight_kg", "29463-7", "Body weight", "kg"),
    ("height_cm", "8302-2", "Body height", "cm"),
    ("bmi", "39156-5", "Body mass index (BMI) [Ratio]", "kg/m2"),
]
SEARCHABLE = ("AllergyIntolerance", "Condition", "MedicationStatement", "Observation", "Encounter")


def _concept(system: str | None, code: str | None, text: str) -> dict:
    concept: dict = {"text": text}
    if code:
        concept["coding"] = [{"system": system, "code": code, "display": text}]
    return concept


class Fhir:
    def __init__(self, chart: Chart, timezone: str = "America/Los_Angeles"):
        self.chart = chart
        self.Session = chart.Session
        self.tz = ZoneInfo(timezone)

    def _dt(self, value: datetime) -> str:
        """Clinic-local time with its UTC offset, as FHIR requires when a time is given."""
        return value.replace(tzinfo=self.tz).isoformat()

    # ------------------------------------------------------------------ resources
    def _patient(self, p: Patient) -> dict:
        given, _, family = p.name.partition(" ")
        out = {"resourceType": "Patient", "id": str(p.id),
               "name": [{"use": "official", "text": p.name, "family": family or given, "given": [given]}]}
        if p.phone and p.phone != "-":
            out["telecom"] = [{"system": "phone", "value": p.phone}]
        if p.sex in ("female", "male", "other"):
            out["gender"] = p.sex
        if p.birth_date:
            out["birthDate"] = p.birth_date.isoformat()
        return out

    def _allergy(self, a: Allergy) -> dict:
        out = {
            "resourceType": "AllergyIntolerance", "id": str(a.id),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                           "code": a.status}]},
            "verificationStatus": {"coding": [{"system": ALLERGY_VERIFICATION, "code": "confirmed"}]},
            "type": "allergy",
            "code": _concept(SNOMED, a.code, a.substance),
            "patient": {"reference": f"Patient/{a.patient_id}"},
        }
        if a.category:
            out["category"] = [a.category]
        if a.criticality:
            out["criticality"] = a.criticality
        if a.recorded_on:
            out["recordedDate"] = a.recorded_on.isoformat()
        if a.reaction:
            out["reaction"] = [{"manifestation": [{"text": a.reaction}]}]
        return out

    def _condition(self, c: Condition) -> dict:
        out = {
            "resourceType": "Condition", "id": str(c.id),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": c.status}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                                               "code": "confirmed"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                      "code": "problem-list-item"}]}],
            "code": _concept(SNOMED, c.code, c.name),
            "subject": {"reference": f"Patient/{c.patient_id}"},
        }
        if c.onset:
            out["onsetDateTime"] = c.onset.isoformat()
        if c.resolved:
            out["abatementDateTime"] = c.resolved.isoformat()
        return out

    def _medication(self, m: Medication) -> dict:
        out = {
            "resourceType": "MedicationStatement", "id": str(m.id),
            "status": "active" if m.status == "active" else "stopped",
            "medicationCodeableConcept": _concept(RXNORM, m.code, m.name),
            "subject": {"reference": f"Patient/{m.patient_id}"},
        }
        period = {}
        if m.started:
            period["start"] = m.started.isoformat()
        if m.stopped:
            period["end"] = m.stopped.isoformat()
        if period:
            out["effectivePeriod"] = period
        if m.dosage:
            out["dosage"] = [{"text": m.dosage}]
        return out

    def _observations(self, v: Vitals) -> list[dict]:
        base = {"resourceType": "Observation", "status": "final", "category": [VITAL_SIGNS],
                "subject": {"reference": f"Patient/{v.patient_id}"}, "effectiveDateTime": self._dt(v.taken_at)}
        if v.appointment_id:
            base["encounter"] = {"reference": f"Encounter/{v.appointment_id}"}
        out = []
        if v.systolic is not None and v.diastolic is not None:
            out.append({**base, "id": f"{v.id}-bp",
                        "code": {"coding": [{"system": LOINC, "code": "85354-9", "display": "Blood pressure panel"}],
                                 "text": "Blood pressure"},
                        "component": [
                            {"code": {"coding": [{"system": LOINC, "code": code, "display": label}]},
                             "valueQuantity": {"value": value, "unit": "mm[Hg]", "system": UCUM, "code": "mm[Hg]"}}
                            for code, label, value in (("8480-6", "Systolic blood pressure", v.systolic),
                                                       ("8462-4", "Diastolic blood pressure", v.diastolic))]})
        for field, code, label, unit in SIMPLE_VITALS:
            value = getattr(v, field)
            if value is not None:
                out.append({**base, "id": f"{v.id}-{field.replace('_', '-')}",
                            "code": {"coding": [{"system": LOINC, "code": code, "display": label}], "text": label},
                            "valueQuantity": {"value": value, "unit": unit, "system": UCUM, "code": unit}})
        return out

    def _encounter(self, a: Appointment, slot: Slot, provider: Provider) -> dict:
        past = slot.end <= self.chart.clock.now()
        status = {"completed": "finished", "no_show": "cancelled", "cancelled": "cancelled",
                  "booked": "finished" if past else "planned", "held": "planned"}[a.status]
        return {
            "resourceType": "Encounter", "id": str(a.id), "status": status,
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB",
                      "display": "ambulatory"},
            "subject": {"reference": f"Patient/{a.patient_id}"},
            "participant": [{"individual": {"display": provider.name}}],
            "serviceType": {"text": provider.specialty},
            "period": {"start": self._dt(slot.start), "end": self._dt(slot.end)},
        }

    # ------------------------------------------------------------------ reads
    def _bundle(self, base: str, resources: list[dict], kind: str = "searchset") -> dict:
        return {"resourceType": "Bundle", "type": kind, "total": len(resources),
                "entry": [{"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": r,
                           "search": {"mode": "match"}} for r in resources]}

    def patient(self, user: dict, patient_id: int) -> dict:
        with self.Session() as s:
            p = self.chart._check(s, user, patient_id)
            self.chart._audit(s, user, "fhir_read", patient_id, "Patient")
            s.commit()
            return self._patient(p)

    def search(self, user: dict, resource_type: str, patient_id: int, base: str) -> dict:
        if resource_type not in SEARCHABLE:
            raise ValueError(f"{resource_type} isn't supported here")
        with self.Session() as s:
            self.chart._check(s, user, patient_id)
            resources = self._resources(s, user, resource_type, patient_id)
            self.chart._audit(s, user, "fhir_read", patient_id, resource_type)
            s.commit()
        return self._bundle(base, resources)

    def everything(self, user: dict, patient_id: int, base: str) -> dict:
        with self.Session() as s:
            p = self.chart._check(s, user, patient_id)
            resources = [self._patient(p)]
            for kind in SEARCHABLE:
                resources += self._resources(s, user, kind, patient_id)
            self.chart._audit(s, user, "fhir_read", patient_id, "$everything")
            s.commit()
        return self._bundle(base, resources)

    def _resources(self, s, user: dict, kind: str, patient_id: int) -> list[dict]:
        if kind == "AllergyIntolerance":
            return [self._allergy(a) for a in s.scalars(select(Allergy).where(Allergy.patient_id == patient_id)
                                                        .order_by(Allergy.id))]
        if kind == "Condition":
            return [self._condition(c) for c in s.scalars(select(Condition).where(Condition.patient_id == patient_id)
                                                          .order_by(Condition.id))]
        if kind == "MedicationStatement":
            return [self._medication(m) for m in s.scalars(select(Medication).where(Medication.patient_id == patient_id)
                                                           .order_by(Medication.id))]
        if kind == "Observation":
            out: list[dict] = []
            for v in s.scalars(select(Vitals).where(Vitals.patient_id == patient_id).order_by(Vitals.taken_at)):
                out += self._observations(v)
            return out
        q = (select(Appointment, Slot, Provider).join(Slot, Appointment.slot_id == Slot.id)
             .join(Provider, Slot.provider_id == Provider.id)
             .where(Appointment.patient_id == patient_id,
                    Appointment.status.in_(("booked", "held", "completed", "no_show", "cancelled")))
             .order_by(Slot.start))
        if user["role"] == "doctor":
            q = q.where(Slot.provider_id == user["provider_id"])  # a doctor sees their own visits only
        return [self._encounter(a, sl, p) for a, sl, p in s.execute(q)]

    @staticmethod
    def capability(base: str) -> dict:
        return {
            "resourceType": "CapabilityStatement", "status": "active", "date": date.today().isoformat(),
            "kind": "instance", "fhirVersion": "4.0.1", "format": ["json"],
            "software": {"name": "CareQueue"}, "implementation": {"description": "CareQueue FHIR API", "url": base},
            "rest": [{"mode": "server",
                      "security": {"description": "Session cookie from /api/auth/login; patients read their own "
                                                  "record, doctors read their patients'."},
                      "resource": [{"type": "Patient", "interaction": [{"code": "read"}],
                                    "operation": [{"name": "everything",
                                                   "definition": "http://hl7.org/fhir/OperationDefinition/"
                                                                 "Patient-everything"}]}]
                      + [{"type": kind, "interaction": [{"code": "search-type"}],
                          "searchParam": [{"name": "patient", "type": "reference"}]} for kind in SEARCHABLE]}],
        }
