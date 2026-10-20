"""The note check and the pre-visit summary: rules, the AI layer's safety checks, and the API."""
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from carequeue.app import build_services, create_app
from carequeue.clock import FakeClock
from carequeue.config import Settings
from carequeue.llm import Gateway
from carequeue.notecheck import Context, check, follow_up_days, rule_findings
from carequeue.summary import Source, check_line, summarize

from .test_api import WEDNESDAY_11, as_doctor

PENICILLIN = {"substance": "Penicillin", "category": "medication", "reaction": "Hives"}


def kinds(findings):
    return sorted(f["kind"] for f in findings)


# ---------------------------------------------------------------------------- note check: rules
@pytest.mark.parametrize("note, expected", [
    ("BP 1380/88. Viral URI, rest and fluids.", ["impossible_value"]),
    ("Blood pressure 80/120 today. Discussed diet.", ["impossible_value"]),
    ("Temp 9.8, HR 720, weighs 7000 kg. Plan: review labs.", ["impossible_value"] * 3),
    ("Otitis media. Start amoxicillin 500 mg three times daily for 7 days.", ["allergy_conflict"]),
    ("Allergic to penicillin, so azithromycin instead. Plan: review in 3 months.", []),
    ("Take 1/2 tablet at night. BP 124/80 mmHg, stable. Continue current plan.", []),
    ("Recheck BP in 2 weeks; continue lisinopril as before.", ["follow_up_not_booked"]),
    ("ok", ["too_short"]),
])
def test_rule_checks(note, expected):
    assert kinds(rule_findings(note, Context(allergies=[PENICILLIN]))) == sorted(expected)


def test_vitals_in_the_note_must_match_the_ones_entered():
    ctx = Context(entered_vitals={"systolic": 128, "diastolic": 82, "heart_rate": 70})
    out = rule_findings("BP 138/82, HR 70. Hypertension follow-up, continue lisinopril.", ctx)
    assert kinds(out) == ["vitals_mismatch"] and out[0]["quote"] == "138/82"
    assert rule_findings("BP 129/82, HR 72. Stable, continue the plan.", ctx) == []  # within rounding


def test_a_booked_follow_up_is_not_flagged():
    note = "Hypertension. Recheck in 2 weeks, continue lisinopril."
    assert rule_findings(note, Context(booked_in_days=[13])) == []
    assert kinds(rule_findings(note, Context(booked_in_days=[40]))) == ["follow_up_not_booked"]
    assert follow_up_days("f/u 3 wks; come back in a month; review in ten days") == [
        (21, "f/u 3 wks"), (30, "come back in a month"), (10, "review in ten days")]


# ---------------------------------------------------------------------------- note check: the AI layer
def fake_gateway(reply: dict | Exception, seen: list | None = None) -> Gateway:
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"content": json.dumps(reply), "model": "fake-model", "cache": "miss",
                                         "cost_usd": 0.0001})
    return Gateway("http://nexus.test", transport=httpx.MockTransport(handler))


NOTE = ("Left ear infection, red tympanic membrane. Start ciprofloxacin drops in the right ear. "
        "BP 1380/88. Continue metformin.")


def test_ai_findings_are_kept_only_if_they_quote_the_note():
    reply = {"issues": [
        {"type": "contradiction", "quote": "drops in the right ear", "explanation": "Left ear infection, right ear "
                                                                                   "treated."},
        {"type": "chart_mismatch", "quote": "Continue metformin", "explanation": "Metformin is stopped in the chart."},
        {"type": "contradiction", "quote": "patient refused treatment", "explanation": "Not in the note."},  # invented
        {"type": "chart_mismatch", "quote": "1380/88", "explanation": "Odd BP."},  # rules already flagged it
        {"type": "diagnosis", "quote": "Left ear infection", "explanation": "Consider MRI."},  # not an allowed type
    ]}
    seen = []
    ctx = Context(medications=[{"name": "Metformin 500 MG Oral Tablet", "status": "stopped"}])
    out = check(NOTE, ctx, fake_gateway(reply, seen))
    assert out["ai"]["used"] and out["ai"]["model"] == "fake-model" and out["ai"]["dropped"] == 2
    ai = [f for f in out["findings"] if f["source"] == "ai"]
    assert [f["quote"] for f in ai] == ["drops in the right ear", "Continue metformin"]
    assert kinds([f for f in out["findings"] if f["source"] == "rules"]) == ["impossible_value"]
    sent = seen[0]["messages"][1]["content"]
    assert "Stopped medications: Metformin 500 MG Oral Tablet" in sent and "Visit note" in sent


def test_note_check_still_works_when_the_gateway_is_down():
    out = check(NOTE, Context(), fake_gateway(httpx.ConnectError("down")))
    assert out["ai"]["used"] is False and "ConnectError" in out["ai"]["error"]
    assert kinds(out["findings"]) == ["impossible_value"]
    assert check(NOTE, Context(), Gateway(None))["ai"] == {"used": False}


# ---------------------------------------------------------------------------- summary: checking lines
SOURCES = [Source("A1", "allergy", "Penicillin (reaction: Hives), high risk"),
           Source("V1", "vitals", "Sep 8, 2026: BP 138/88, HR 72"),
           Source("V2", "vitals", "Jul 1, 2026: BP 128/82, HR 70"),
           Source("N1", "visit", "Sep 8, 2026: seen. Note: Hypertension, start lisinopril 10 mg. Recheck in 2 weeks.")]


def test_summary_lines_must_cite_real_sources_and_quote_their_numbers():
    by_id = {s.id: s for s in SOURCES}
    ok, why = check_line({"text": "BP rose from 128/82 to 138/88 since July.", "sources": ["V1", "V2"]}, by_id)
    assert ok and why is None and [s["id"] for s in ok["sources"]] == ["V1", "V2"]
    assert check_line({"text": "BP was 150/95 on Sep 8.", "sources": ["V1"]}, by_id)[1].startswith("numbers not")
    assert check_line({"text": "Allergic to penicillin.", "sources": ["X9"]}, by_id)[1] == "no valid source"
    assert check_line({"text": "Started lisinopril 10 mg on Sep 8.", "sources": ["N1"]}, by_id)[0]
    assert check_line({"text": "2 readings this summer.", "sources": ["V1"]}, by_id)[0]  # small counts are fine
    hr = {"V1": Source("V1", "vitals", "Sep 8, 2026: BP 138/88, HR 96 bpm, weight 75.5 kg")}
    assert check_line({"text": "Heart rate 96 bpm, weight 75.5 kg.", "sources": ["V1"]}, hr)[0]
    assert check_line({"text": "Weight 96 kg.", "sources": ["V1"]}, hr)[1].startswith("numbers not")
    up = {"U1": Source("U1", "upcoming", "Booked with you: Fri Oct 16, 2026, 11:30 AM")}
    assert check_line({"text": "Next visit Fri Oct 16, 2026 at 11:30 AM.", "sources": ["U1"]}, up)[0]


def test_summary_drops_made_up_lines_and_falls_back_when_needed():
    reply = {"lines": [
        {"text": "High-risk penicillin allergy (hives).", "sources": ["A1"]},
        {"text": "BP up from 128/82 in July to 138/88 on Sep 8.", "sources": ["V2", "V1"]},
        {"text": "Weight fell to 72 kg.", "sources": ["V1"]},  # made up
    ]}
    out = summarize(SOURCES, [], fake_gateway(reply), 5)
    assert out["kind"] == "ai" and len(out["lines"]) == 2 and len(out["ai"]["dropped"]) == 1
    assert "72" in out["ai"]["dropped"][0]["why"]

    all_bad = summarize(SOURCES, [], fake_gateway({"lines": [{"text": "BP 190/120.", "sources": ["V1"]}]}), 5)
    assert all_bad["kind"] == "basic" and "source check" in all_bad["ai"]["error"]
    down = summarize(SOURCES, [], fake_gateway(httpx.ReadTimeout("slow")), 5)
    assert down["kind"] == "basic" and down["lines"] and "ReadTimeout" in down["ai"]["error"]
    assert summarize([], [], None, 5)["kind"] == "empty"


# ---------------------------------------------------------------------------- through the API
@pytest.fixture
def ai_client():
    """The demo clinic, with NexusAI replaced by a fake that answers every request."""
    def handler(request):
        system = json.loads(request.content)["messages"][0]["content"]
        if "summary" in system:
            facts = json.loads(request.content)["messages"][1]["content"]
            first = facts.split("[", 1)[1].split("]", 1)[0]
            return httpx.Response(200, json={"content": json.dumps(
                {"lines": [{"text": "See the cited record.", "sources": [first]}]}), "model": "fake-model"})
        return httpx.Response(200, json={"content": '{"issues": []}', "model": "fake-model"})
    settings = Settings(database_url="sqlite://", run_background_worker=False, seed_demo=True, demo_logins=True,
                        nexusai_url="http://nexus.test")
    services = build_services(settings, clock=FakeClock(WEDNESDAY_11), transport=httpx.MockTransport(handler))
    client = TestClient(create_app(services))
    client.services = services
    return client


def test_check_note_over_http(ai_client):
    as_doctor(ai_client)
    today = ai_client.get("/api/doctor/today").json()
    visit = next(v for v in today["visits"] if v["past"] and v["status"] == "booked")
    out = ai_client.post(f"/api/doctor/appointments/{visit['id']}/check-note",
                         json={"note": "BP 1380/88, recheck in 1 week.", "vitals": {"systolic": 128, "diastolic": 80}})
    assert out.status_code == 200 and out.json()["ai"]["used"]
    assert "impossible_value" in kinds(out.json()["findings"])
    bad_vitals = ai_client.post(f"/api/doctor/appointments/{visit['id']}/check-note",
                                json={"note": "fine", "vitals": {"systolic": 999}})
    assert bad_vitals.status_code == 400
    with ai_client.services.clinic.Session() as s:
        from sqlalchemy import select

        from carequeue.models import Appointment, Slot
        others = s.scalar(select(Appointment.id).join(Slot).where(Slot.provider_id == 2).limit(1))
    assert ai_client.post(f"/api/doctor/appointments/{others}/check-note", json={"note": "x"}).status_code == 404


def test_summary_over_http(ai_client):
    as_doctor(ai_client)
    pid = ai_client.get("/api/doctor/today").json()["next"]["patient_id"]
    out = ai_client.post(f"/api/doctor/patients/{pid}/summary").json()
    assert out["kind"] == "ai" and out["lines"][0]["sources"][0]["id"]
    log = ai_client.get(f"/api/doctor/patients/{pid}/access-log").json()
    assert any(e["action"] == "summary" for e in log)
    sources = summarize_sources(ai_client, pid)
    assert all("Dr." not in s for s in sources)  # only this doctor's notes are used


def summarize_sources(client, pid):
    from carequeue.summary import gather

    with client.services.clinic.Session() as s:
        sources, _ = gather(s, 1, pid, WEDNESDAY_11)
    visit_ids = {s.id for s in sources if s.kind == "visit"}
    with client.services.clinic.Session() as s:
        from sqlalchemy import func, select

        from carequeue.models import Appointment, Slot
        own = s.scalar(select(func.count()).select_from(Appointment).join(Slot).where(
            Appointment.patient_id == pid, Slot.provider_id == 1, Appointment.status.in_(("completed", "no_show")),
            Slot.start <= datetime(2026, 10, 14, 11, 0)))
    assert len(visit_ids) == min(own, 6)
    return [s.text for s in sources]


def test_adding_a_medication_warns_about_allergies():
    client, services = __import__("tests.test_api", fromlist=["make_client"]).make_client()
    as_doctor(client)
    pid = client.get("/api/doctor/today").json()["next"]["patient_id"]
    client.post(f"/api/doctor/patients/{pid}/allergies", json={"substance": "Penicillin", "category": "medication"})
    out = client.post(f"/api/doctor/patients/{pid}/medications", json={"name": "Amoxicillin 500 MG Oral Capsule"})
    assert out.status_code == 201 and "penicillin family" in out.json()["warnings"][0]
    assert client.post(f"/api/doctor/patients/{pid}/medications",
                       json={"name": "Azithromycin 250 MG Oral Tablet"}).json()["warnings"] == []


def test_ai_flags_that_the_note_itself_disproves_are_dropped():
    """Real false alarms from the first live run: a plan was written, or the chart was simply empty."""
    note = "Eczema flare. Hydrocortisone 1% twice daily. Follow up in 10 days to see if improving."
    reply = {"issues": [
        {"type": "missing_plan", "quote": "Eczema flare", "explanation": "No treatment is written."},
        {"type": "chart_mismatch", "quote": "Eczema flare", "explanation": "The chart lists no eczema."},
    ]}
    out = check(note, Context(booked_in_days=[10]), fake_gateway(reply))
    assert out["findings"] == [] and out["ai"]["dropped"] == 2
    # With a real chart entry to conflict with, a chart mismatch is kept; with no plan at all, so is a missing plan.
    reply = {"issues": [
        {"type": "chart_mismatch", "quote": "Continue glipizide", "explanation": "Glipizide is stopped."},
        {"type": "missing_plan", "quote": "pneumonia", "explanation": "No treatment for pneumonia."},
    ]}
    ctx = Context(medications=[{"name": "glipizide 5 MG Oral Tablet", "status": "stopped"}])
    out = check("Crackles at right base. Assessment: pneumonia. Continue glipizide.", ctx, fake_gateway(reply))
    assert [f["kind"] for f in out["findings"]] == ["chart_mismatch"]  # "continue" is a plan word
    out = check("Cough and fever, crackles at right base. Assessment: pneumonia.", ctx, fake_gateway(reply))
    assert [f["kind"] for f in out["findings"]] == ["missing_plan"]  # the glipizide quote isn't in this note
