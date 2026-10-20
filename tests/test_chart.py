"""The patient chart, its audit log, vitals at visits, and the FHIR API."""
import pytest

from .test_api import as_doctor, as_patient, make_client

fhir_models = pytest.importorskip("fhir.resources.R4B", reason="fhir.resources is a dev dependency")
from fhir.resources.R4B import get_fhir_model_class  # noqa: E402


@pytest.fixture
def client():
    client, services = make_client()
    client.services = services
    return client


def harper_id(client) -> int:
    as_patient(client)
    pid = client.get("/api/auth/me").json()["patient_id"]
    client.post("/api/auth/logout")
    return pid


def someone_not_seen_by_patel(client) -> int:
    from sqlalchemy import exists, select

    from carequeue.models import Appointment, Patient, Slot, WaitlistEntry

    with client.services.clinic.Session() as s:
        seen = exists().where(Appointment.patient_id == Patient.id, Appointment.slot_id == Slot.id,
                              Slot.provider_id == 1)
        waiting = exists().where(WaitlistEntry.patient_id == Patient.id, WaitlistEntry.provider_id == 1)
        return s.scalar(select(Patient.id).where(~seen, ~waiting).limit(1))


def test_the_demo_patient_has_a_full_synthea_chart(client):
    pid = harper_id(client)
    as_patient(client)
    chart = client.get("/api/me/chart").json()
    assert chart["patient"]["sex"] == "female" and 18 <= chart["patient"]["age"] <= 90
    assert any(a["status"] == "active" for a in chart["allergies"])
    assert any(m["status"] == "active" for m in chart["medications"]) and chart["conditions"]
    assert chart["vitals"] and chart["vitals"][0]["systolic"]
    assert all(a["source"] == "synthea" for a in chart["allergies"])
    assert chart["patient"]["id"] == pid


def test_who_can_see_a_chart(client):
    pid = harper_id(client)
    stranger = someone_not_seen_by_patel(client)
    as_doctor(client)
    assert client.get(f"/api/doctor/patients/{pid}/chart").status_code == 200
    assert client.get(f"/api/doctor/patients/{stranger}/chart").status_code == 404  # not Dr. Patel's patient
    client.post("/api/auth/logout")
    as_patient(client)
    assert client.get(f"/api/doctor/patients/{pid}/chart").status_code == 403  # patients use /api/me/chart
    assert client.get("/api/me/chart").json()["patient"]["id"] == pid
    client.post("/api/auth/logout")
    assert client.get("/api/me/chart").status_code == 401


def test_doctor_edits_are_checked_and_logged(client):
    pid = harper_id(client)
    as_doctor(client)
    added = client.post(f"/api/doctor/patients/{pid}/allergies",
                        json={"substance": "Penicillin", "reaction": "Hives", "criticality": "high",
                              "category": "medication"})
    assert added.status_code == 201
    again = client.post(f"/api/doctor/patients/{pid}/allergies", json={"substance": "penicillin"})
    assert again.status_code == 409
    bad = client.post(f"/api/doctor/patients/{pid}/allergies", json={"substance": "Latex", "criticality": "very"})
    assert bad.status_code == 400
    aid = added.json()["id"]
    assert client.post(f"/api/doctor/allergies/{aid}/status", json={"status": "inactive"}).json()["status"] \
        == "inactive"

    cond = client.post(f"/api/doctor/patients/{pid}/conditions", json={"name": "Seasonal allergic rhinitis"})
    assert client.post(f"/api/doctor/conditions/{cond.json()['id']}/resolve").json()["status"] == "resolved"
    assert client.post(f"/api/doctor/conditions/{cond.json()['id']}/resolve").status_code == 409
    future = client.post(f"/api/doctor/patients/{pid}/conditions", json={"name": "Flu", "onset": "2030-01-01"})
    assert future.status_code == 400

    med = client.post(f"/api/doctor/patients/{pid}/medications",
                      json={"name": "Cetirizine 10 MG Oral Tablet", "dosage": "1 tablet daily"})
    assert client.post(f"/api/doctor/medications/{med.json()['id']}/stop").json()["status"] == "stopped"

    chart = client.get(f"/api/doctor/patients/{pid}/chart").json()
    pen = next(a for a in chart["allergies"] if a["substance"] == "Penicillin")
    assert pen["recorded_by"] == "Dr. Maya Patel" and pen["source"] == "clinic" and pen["reaction"] == "Hives"
    actions = [e["action"] for e in client.get(f"/api/doctor/patients/{pid}/access-log").json()]
    for action in ("add_allergy", "allergy_inactive", "add_condition", "resolve_condition", "add_medication",
                   "stop_medication", "view_chart"):
        assert action in actions, action

    client.post("/api/auth/logout")
    as_patient(client)
    log = client.get("/api/me/access-log").json()
    assert any(e["actor"] == "Dr. Maya Patel" and e["action"] == "view_chart" for e in log)


def test_vitals_are_recorded_when_a_visit_is_closed(client):
    as_doctor(client)
    today = client.get("/api/doctor/today").json()
    assert "chart" in today["next"] and set(today["next"]["chart"]) == {"allergies", "conditions", "medications"}
    visit = next(v for v in today["visits"] if v["past"] and v["status"] == "booked")

    typo = client.post(f"/api/doctor/appointments/{visit['id']}/complete",
                       json={"note": "Checked.", "vitals": {"systolic": 1380, "diastolic": 88}})
    assert typo.status_code == 400 and "isn't possible" in typo.json()["error"]
    still_open = client.get("/api/doctor/today").json()["visits"]
    assert next(v for v in still_open if v["id"] == visit["id"])["status"] == "booked"  # nothing was saved

    upside_down = client.post(f"/api/doctor/appointments/{visit['id']}/complete",
                              json={"vitals": {"systolic": 80, "diastolic": 120}})
    assert upside_down.status_code == 400

    closed = client.post(f"/api/doctor/appointments/{visit['id']}/complete",
                         json={"note": "Checked.", "vitals": {"systolic": 128, "diastolic": 82, "heart_rate": 70,
                                                              "weight_kg": 70, "height_cm": 175}}).json()
    assert closed["status"] == "completed" and closed["vitals"]["bmi"] == 22.9
    fixed = client.put(f"/api/doctor/appointments/{visit['id']}/vitals", json={"systolic": 130}).json()
    assert fixed["systolic"] == 130 and fixed["diastolic"] == 82  # one value corrected, the rest kept
    chart = client.get(f"/api/doctor/patients/{visit['patient_id']}/chart").json()
    assert any(v["appointment_id"] == visit["id"] for v in chart["vitals"])


# The models check structure, required fields, types and time zones, but not every code's
# value set, so the codes this server uses are checked here against the FHIR R4 value sets.
VALUE_SETS = {
    "AllergyIntolerance": lambda r: r["clinicalStatus"]["coding"][0]["code"] in ("active", "inactive", "resolved"),
    "Condition": lambda r: r["clinicalStatus"]["coding"][0]["code"] in ("active", "recurrence", "relapse",
                                                                        "inactive", "remission", "resolved"),
    "MedicationStatement": lambda r: r["status"] in ("active", "completed", "entered-in-error", "intended",
                                                     "stopped", "on-hold", "unknown", "not-taken"),
    "Observation": lambda r: r["status"] == "final"
    and r["category"][0]["coding"][0]["code"] == "vital-signs",
    "Encounter": lambda r: r["status"] in ("planned", "arrived", "triaged", "in-progress", "onleave",
                                           "finished", "cancelled", "entered-in-error", "unknown"),
}


def validate(resource: dict):
    """Parse with the official FHIR R4B models; raises if anything isn't valid FHIR."""
    check = VALUE_SETS.get(resource["resourceType"])
    assert check is None or check(resource), resource
    return get_fhir_model_class(resource["resourceType"]).model_validate(resource)


def test_fhir_api_serves_valid_fhir(client):
    pid = harper_id(client)
    meta = client.get("/fhir/metadata")
    assert meta.headers["content-type"].startswith("application/fhir+json")
    validate(meta.json())
    as_patient(client)
    validate(client.get(f"/fhir/Patient/{pid}").json())
    for kind in ("AllergyIntolerance", "Condition", "MedicationStatement", "Observation", "Encounter"):
        bundle = client.get(f"/fhir/{kind}", params={"patient": f"Patient/{pid}"}).json()
        validate(bundle)
        assert bundle["total"] > 0, kind
        for entry in bundle["entry"]:
            assert entry["resource"]["resourceType"] == kind
            validate(entry["resource"])
    everything = client.get(f"/fhir/Patient/{pid}/$everything").json()
    validate(everything)
    kinds = {e["resource"]["resourceType"] for e in everything["entry"]}
    assert kinds == {"Patient", "AllergyIntolerance", "Condition", "MedicationStatement", "Observation", "Encounter"}
    bp = next(e["resource"] for e in everything["entry"] if e["resource"]["id"].endswith("-bp"))
    assert {c["code"]["coding"][0]["code"] for c in bp["component"]} == {"8480-6", "8462-4"}  # LOINC
    assert bp["effectiveDateTime"][-6:] in ("-07:00", "-08:00")  # clinic time, with its offset


def test_fhir_access_rules_and_errors(client):
    pid = harper_id(client)
    stranger = someone_not_seen_by_patel(client)
    out = client.get(f"/fhir/Patient/{pid}")
    assert out.status_code == 401 and out.json()["resourceType"] == "OperationOutcome"
    as_doctor(client)
    assert client.get(f"/fhir/Patient/{stranger}").status_code == 404
    encounters = client.get("/fhir/Encounter", params={"patient": pid}).json()
    assert all(e["resource"]["participant"][0]["individual"]["display"] == "Dr. Maya Patel"
               for e in encounters["entry"])  # a doctor sees only their own visits
    assert client.get("/fhir/Practitioner", params={"patient": pid}).status_code == 404
    assert client.get("/fhir/Condition").status_code == 400
    log = client.get(f"/api/doctor/patients/{pid}/access-log").json()
    assert any(e["action"] == "fhir_read" and e["detail"] == "Encounter" for e in log)
