"""A short pre-visit summary of a patient, for their doctor.

Built from the patient's chart (allergies, conditions, medications, vitals) and this
doctor's own visit notes. Other doctors' notes stay private, as everywhere else.

Every fact in the record is given an id (A1 = an allergy, V2 = a set of vitals, N1 = a
visit note, ...), and the model must cite the ids each line is based on. Then every line
is checked before the doctor sees it:
- it must cite at least one real id;
- every number in it (138/88, 2 weeks, Sep 8) must appear in the sources it cites.
Lines that fail are dropped and counted, so a made-up reading or date can't reach the
doctor. If NexusAI isn't available, a plain summary built directly from the record is
shown instead, so the button always works.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select

from .clinic import fmt_when
from .llm import Gateway, GatewayUnavailable
from .models import Allergy, Appointment, Condition, Medication, Slot, Vitals
from .notecheck import follow_up_days

MAX_LINES = 6
NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A number with a unit, or a reading like 138/88, must appear in the sources *with* that unit:
# "72 kg" isn't supported by a source that only says "HR 72".
MEASURE = re.compile(r"\b(\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:kg|mg|mcg|ml|°\s*c|%|bpm|mmhg)\b)",
                     re.I)
FREE_NUMBERS = {str(n) for n in range(0, 11)}  # small counts ("2 missed visits") needn't be quoted


@dataclass
class Source:
    id: str
    kind: str  # allergy | condition | medication | stopped | vitals | visit | upcoming | open
    text: str

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text}


def _d(value: date | datetime) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _vitals_text(v: Vitals) -> str:
    parts = []
    if v.systolic and v.diastolic:
        parts.append(f"BP {v.systolic:g}/{v.diastolic:g}")
    for value, label, unit in ((v.heart_rate, "HR", " bpm"), (v.temperature_c, "temp", " °C"),
                               (v.weight_kg, "weight", " kg")):
        if value:
            parts.append(f"{label} {value:g}{unit}")
    return f"{_d(v.taken_at)}: " + (", ".join(parts) or "no readings")


def gather(session, provider_id: int, patient_id: int, now: datetime) -> tuple[list[Source], list[dict]]:
    """The numbered facts the summary may use, and open items found without any AI."""
    sources: list[Source] = []

    def add(prefix: str, kind: str, text: str) -> Source:
        n = sum(1 for s in sources if s.id.startswith(prefix) and s.id[len(prefix):].isdigit()) + 1
        src = Source(f"{prefix}{n}", kind, text)
        sources.append(src)
        return src

    for a in session.scalars(select(Allergy).where(Allergy.patient_id == patient_id, Allergy.status == "active")
                             .order_by(Allergy.substance)):
        add("A", "allergy", a.substance + (f" (reaction: {a.reaction})" if a.reaction else "")
            + (", high risk" if a.criticality == "high" else ""))
    for c in session.scalars(select(Condition).where(Condition.patient_id == patient_id,
                                                     Condition.status == "active").order_by(Condition.onset.desc())):
        add("C", "condition", c.name + (f", since {_d(c.onset)}" if c.onset else ""))
    meds = session.scalars(select(Medication).where(Medication.patient_id == patient_id)
                           .order_by(Medication.started.desc())).all()
    for m in meds:
        if m.status == "active":
            add("M", "medication", m.name + (f", {m.dosage}" if m.dosage else "")
                + (f", since {_d(m.started)}" if m.started else ""))
    for m in meds:
        if m.status != "active" and m.stopped and m.stopped >= (now - timedelta(days=365)).date():
            add("S", "stopped", f"{m.name}, stopped {_d(m.stopped)}")
    vitals = session.scalars(select(Vitals).where(Vitals.patient_id == patient_id, Vitals.taken_at <= now)
                             .order_by(Vitals.taken_at.desc()).limit(5)).all()
    for v in vitals:
        add("V", "vitals", _vitals_text(v))
    visits = session.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id).where(
        Appointment.patient_id == patient_id, Slot.provider_id == provider_id,
        Appointment.status.in_(("completed", "no_show")), Slot.start <= now).order_by(Slot.start.desc()).limit(6)).all()
    visit_sources = []
    for a, sl in visits:
        if a.status == "no_show":
            visit_sources.append((add("N", "visit", f"{_d(sl.start)}: did not come"), a, sl))
        else:
            visit_sources.append((add("N", "visit", f"{_d(sl.start)}: seen. Note: {a.note or 'no note'}"), a, sl))
    upcoming = session.execute(select(Appointment, Slot).join(Slot, Appointment.slot_id == Slot.id).where(
        Appointment.patient_id == patient_id, Slot.provider_id == provider_id, Appointment.status == "booked",
        Slot.start > now).order_by(Slot.start).limit(2)).all()
    for _a, sl in upcoming:
        add("U", "upcoming", f"Booked with you: {sl.start:%a} {_d(sl.start)}, {fmt_when(sl.start).split(', ')[-1]}")

    # Open items, found with plain code so they're always right.
    open_items: list[dict] = []
    seen_visit = next(((src, a, sl) for src, a, sl in visit_sources if a.status == "completed"), None)
    if seen_visit and seen_visit[1].note:
        src, _a, sl = seen_visit
        for days, quote in follow_up_days(seen_visit[1].note):
            due = sl.start + timedelta(days=days)
            booked = any(sl.start < s.start <= due + timedelta(days=7) for _x, s in upcoming)
            if days <= 60 and not booked:
                state = "was due" if due < now else "is due"
                open_items.append({"text": f"Follow-up \"{quote.strip()}\" from {_d(sl.start)} {state} by "
                                           f"{_d(due)}, and nothing is booked.", "sources": [src.id]})
    if vitals and vitals[0].systolic and vitals[0].diastolic and (vitals[0].systolic >= 140
                                                                  or vitals[0].diastolic >= 90):
        open_items.append({"text": f"Last blood pressure was high: {vitals[0].systolic:g}/{vitals[0].diastolic:g}.",
                           "sources": [s.id for s in sources if s.kind == "vitals"][:1]})
    missed = [src for src, a, _sl in visit_sources if a.status == "no_show"]
    if missed:
        open_items.append({"text": f"Missed {len(missed)} of the last {len(visit_sources)} visits with you.",
                           "sources": [s.id for s in missed]})
    return sources, open_items


# ---------------------------------------------------------------------------- checking the model's lines
def _numbers(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in NUMBER.findall(text)}


def _measures(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m.lower()).replace("°", "") for m in MEASURE.findall(text)}


def check_line(line: dict, by_id: dict[str, Source]) -> tuple[dict | None, str | None]:
    """The line with its sources, or why it was dropped."""
    text = str(line.get("text") or "").strip()
    ids = [str(i).strip().strip("[]") for i in (line.get("sources") or []) if str(i).strip()]
    cited = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    if not text:
        return None, "empty"
    if not cited:
        return None, "no valid source"
    allowed = set().union(*(_numbers(s.text) for s in cited))
    made_up = (_numbers(text) - allowed - FREE_NUMBERS) | (_measures(text) - set().union(
        *(_measures(s.text) for s in cited)))
    if made_up:
        return None, f"numbers not in its sources: {', '.join(sorted(made_up))}"
    return {"text": text[:240], "sources": [s.public() for s in cited]}, None


SYSTEM_PROMPT = """You write a short pre-visit summary of a patient for their doctor, from the numbered facts given.
Rules:
- Use only the facts given. Do not add diagnoses, advice, or anything not in the facts.
- 3 to 6 short lines, most useful first: allergies that matter, ongoing problems and current treatment,
  how recent vitals compare, what happened at recent visits, and anything still open.
- Every line must list the ids of the facts it is based on.
- Copy numbers and dates exactly as they appear in the facts.
Reply with JSON only: {"lines": [{"text": "one short sentence", "sources": ["V1", "N2"]}]}"""


def ai_summary(gateway: Gateway, sources: list[Source], open_items: list[dict], timeout_s: float) -> tuple[list, dict]:
    facts = "\n".join(f"[{s.id}] ({s.kind}) {s.text}" for s in sources)
    if open_items:
        facts += "\n\nAlready flagged by the system (no need to repeat):\n" + "\n".join(
            f"- {o['text']}" for o in open_items)
    data, meta = gateway.ask_json(SYSTEM_PROMPT, f"Facts:\n{facts}", timeout_s, max_tokens=700)
    lines = data.get("lines")
    if not isinstance(lines, list):
        raise GatewayUnavailable("the reply had no lines list")
    by_id = {s.id: s for s in sources}
    kept, dropped = [], []
    for line in lines[:MAX_LINES + 2]:
        ok, why = check_line(line if isinstance(line, dict) else {}, by_id)
        if ok:
            kept.append(ok)
        else:
            dropped.append({"text": str(line.get("text", ""))[:240] if isinstance(line, dict) else "", "why": why})
    return kept[:MAX_LINES], {**meta, "dropped": dropped}


def basic_summary(sources: list[Source]) -> list[dict]:
    """A summary built straight from the record, used when NexusAI isn't available."""
    def of(kind: str) -> list[Source]:
        return [s for s in sources if s.kind == kind]

    def line(text: str, cited: list[Source]) -> dict:
        return {"text": text, "sources": [s.public() for s in cited]}

    out = []
    allergies, conditions, meds = of("allergy"), of("condition"), of("medication")
    if allergies:
        out.append(line("Allergies: " + ", ".join(s.text.split(" (")[0].split(",")[0] for s in allergies) + ".",
                        allergies))
    if conditions:
        names = [s.text.split(", since")[0] for s in conditions]
        more = f" and {len(names) - 4} more" if len(names) > 4 else ""
        out.append(line("Ongoing: " + ", ".join(names[:4]) + more + ".", conditions))
    if meds:
        out.append(line(f"On {len(meds)} medication{'s' if len(meds) != 1 else ''}: "
                        + ", ".join(s.text.split(",")[0] for s in meds[:3]) + ("…" if len(meds) > 3 else "."), meds))
    vitals = of("vitals")
    if vitals:
        prev = f" Before that, {vitals[1].text}." if len(vitals) > 1 else ""
        out.append(line(f"Latest vitals, {vitals[0].text}.{prev}", vitals[:2]))
    visits = of("visit")
    if visits:
        out.append(line(f"Last visit with you, {visits[0].text}", visits[:1]))
    upcoming = of("upcoming")
    if upcoming:
        out.append(line(upcoming[0].text + ".", upcoming[:1]))
    return out[:MAX_LINES]


def summarize(sources: list[Source], open_items: list[dict], gateway: Gateway | None, timeout_s: float) -> dict:
    by_id = {s.id: s for s in sources}
    items = [{"text": o["text"], "sources": [by_id[i].public() for i in o["sources"] if i in by_id]}
             for o in open_items]
    if not sources:
        return {"lines": [], "open_items": items, "kind": "empty", "ai": {"used": False}}
    if gateway is not None and gateway.enabled:
        try:
            lines, meta = ai_summary(gateway, sources, open_items, timeout_s)
            if lines:
                return {"lines": lines, "open_items": items, "kind": "ai", "ai": {"used": True, **meta}}
            ai = {"used": False, "error": "every line failed the source check", **meta}
        except GatewayUnavailable as exc:
            ai = {"used": False, "error": str(exc)}
    else:
        ai = {"used": False}
    return {"lines": basic_summary(sources), "open_items": items, "kind": "basic", "ai": ai}
