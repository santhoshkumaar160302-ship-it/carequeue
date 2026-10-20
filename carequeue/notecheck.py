"""Check a visit note for likely mistakes before the doctor saves it.

Two layers, so the check is useful even without the AI:

1. Rules (instant, always the same answer):
   - numbers that can't be real ("BP 1380/88", "temp 9.8", "HR 720");
   - vitals in the note that disagree with the vitals entered for the visit;
   - a drug the patient is allergic to, or a drug in the same family (amoxicillin
     when the chart says penicillin);
   - "follow up in 2 weeks" with nothing booked;
   - a note too short to be useful.
2. The LLM, through NexusAI, for what rules can't catch: the note contradicting itself
   (left ear / right ear), contradicting the chart (continuing a drug the chart says was
   stopped), or assessing a problem with no plan.

The check only flags; it never changes the note, and the doctor can save anyway. Every
AI finding must quote the note word for word, and findings whose quote isn't in the note
are thrown away, so the model can't invent problems in text that isn't there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from .chart import VITAL_LIMITS
from .clinic import NotFound
from .llm import Gateway, GatewayUnavailable
from .models import Allergy, Appointment, Condition, Medication, Slot, Vitals

AI_TYPES = ("contradiction", "chart_mismatch", "missing_plan")
MAX_AI_ISSUES = 4

# Drug families: an allergy to one member is a warning for the others.
DRUG_FAMILIES = {
    "penicillin": ("penicillin", "amoxicillin", "ampicillin", "augmentin", "piperacillin", "dicloxacillin",
                   "nafcillin", "oxacillin"),
    "cephalosporin": ("cephalexin", "keflex", "cefuroxime", "ceftriaxone", "cefdinir", "cefazolin", "cefpodoxime"),
    "sulfonamide": ("sulfamethoxazole", "bactrim", "septra", "sulfasalazine"),
    "NSAID": ("aspirin", "ibuprofen", "naproxen", "diclofenac", "advil", "motrin", "aleve", "ketorolac",
              "celecoxib", "meloxicam"),
    "opioid": ("codeine", "morphine", "hydrocodone", "oxycodone", "tramadol"),
    "macrolide": ("erythromycin", "azithromycin", "clarithromycin", "z-pack"),
    "fluoroquinolone": ("ciprofloxacin", "levofloxacin", "moxifloxacin"),
    "tetracycline": ("tetracycline", "doxycycline", "minocycline"),
}
# What to look for in an allergy's name to know its family.
ALLERGY_FAMILY_HINTS = {
    "penicillin": ("penicillin", "amoxicillin", "ampicillin"),
    "cephalosporin": ("cephalosporin", "cephalexin", "cef"),
    "sulfonamide": ("sulfa", "sulfamethoxazole", "sulfonamide", "trimethoprim"),
    "NSAID": ("aspirin", "nsaid", "ibuprofen", "naproxen"),
    "opioid": ("codeine", "morphine", "opioid", "opiate"),
    "macrolide": ("erythromycin", "azithromycin", "macrolide"),
    "fluoroquinolone": ("ciprofloxacin", "levofloxacin", "quinolone"),
    "tetracycline": ("tetracycline", "doxycycline"),
}
NEGATION = re.compile(r"\b(allerg\w*|avoid\w*|not|no|instead|contraindicated|never|stop\w*|discontinu\w*)\b", re.I)
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10}
UNIT_DAYS = {"d": 1, "w": 7, "m": 30}
FOLLOW_UP = re.compile(
    r"\b(?:recheck|re-check|follow[\s-]*up|f/u|review|come back|return|see (?:her|him|them) again)\b[^.;\n]{0,25}?"
    r"\b(?:in\s+)?(\d+|a|an|one|two|three|four|five|six|ten)\s*-?\s*"
    r"(d|days?|w|wks?|weeks?|mo|months?)\b", re.I)


@dataclass
class Context:
    """Everything the check needs to know besides the note. Built from the database, or by the eval."""

    allergies: list[dict] = field(default_factory=list)  # {"substance", "category", "reaction"}
    medications: list[dict] = field(default_factory=list)  # {"name", "status"}
    conditions: list[str] = field(default_factory=list)  # active only
    last_vitals: dict | None = None  # {"label", "systolic", ...}
    entered_vitals: dict = field(default_factory=dict)  # this visit's vitals, if the doctor typed any
    booked_in_days: list[int] = field(default_factory=list)  # future visits with this doctor, days after this one


def finding(kind: str, message: str, quote: str | None = None, severity: str = "warning",
            source: str = "rules") -> dict:
    return {"kind": kind, "severity": severity, "message": message, "quote": quote, "source": source}


# ---------------------------------------------------------------------------- rules
def _num(value: str) -> float:
    return float(value)


def _impossible(note: str, entered: dict) -> list[dict]:
    out = []
    # Blood pressure: only with a BP word nearby or "mmHg" after, so "1/2 tablet" isn't read as a BP.
    for m in re.finditer(r"(?:\bbp\b|\bb/p\b|blood pressure)\D{0,12}?(\d{1,4})\s*/\s*(\d{1,4})\b|"
                         r"(\d{2,4})\s*/\s*(\d{2,4})\s*mm\s*hg", note, re.I):
        sys_, dia = _num(m.group(1) or m.group(3)), _num(m.group(2) or m.group(4))
        quote = f"{m.group(1) or m.group(3)}/{m.group(2) or m.group(4)}"
        lo_s, hi_s, _ = VITAL_LIMITS["systolic"]
        lo_d, hi_d, _ = VITAL_LIMITS["diastolic"]
        if not (lo_s <= sys_ <= hi_s and lo_d <= dia <= hi_d) or dia >= sys_:
            out.append(finding("impossible_value", f"Blood pressure {quote} can't be right. A typo?", quote))
        elif entered.get("systolic") and entered.get("diastolic") and (
                abs(entered["systolic"] - sys_) > 2 or abs(entered["diastolic"] - dia) > 2):
            out.append(finding("vitals_mismatch", f"The note says {quote}, but the vitals entered for this visit "
                               f"say {entered['systolic']:g}/{entered['diastolic']:g}.", quote))
    for m in re.finditer(r"(?:\bhr\b|heart rate|pulse)\D{0,8}?(\d{1,4})|(\d{1,4})\s*bpm\b", note, re.I):
        value = _num(m.group(1) or m.group(2))
        lo, hi, _ = VITAL_LIMITS["heart_rate"]
        if not lo <= value <= hi:
            out.append(finding("impossible_value", f"A heart rate of {value:g} can't be right. A typo?",
                               m.group(1) or m.group(2)))
        elif entered.get("heart_rate") and abs(entered["heart_rate"] - value) > 5:
            out.append(finding("vitals_mismatch", f"The note says a heart rate of {value:g}, but the vitals entered "
                               f"say {entered['heart_rate']:g}.", m.group(1) or m.group(2)))
    for m in re.finditer(r"\btemp(?:erature)?\b\D{0,6}?(\d{1,3}(?:\.\d+)?)\s*(°\s*[cf]|[cf]\b)?", note, re.I):
        value, unit = _num(m.group(1)), (m.group(2) or "").replace("°", "").strip().lower()
        ok = 30 <= value <= 44 if unit == "c" else 86 <= value <= 111 if unit == "f" else (
            30 <= value <= 44 or 86 <= value <= 111)
        if not ok:
            out.append(finding("impossible_value", f"A temperature of {m.group(1)}{' ' + unit.upper() if unit else ''}"
                               " can't be right. A typo?", m.group(1)))
    for m in re.finditer(r"(\d{1,4}(?:\.\d+)?)\s*kg\b", note, re.I):
        value = _num(m.group(1))
        lo, hi, _ = VITAL_LIMITS["weight_kg"]
        if not lo <= value <= hi:
            out.append(finding("impossible_value", f"A weight of {value:g} kg can't be right. A typo?", m.group(0)))
    return out


def allergy_family(substance: str) -> str | None:
    s = substance.lower()
    for family, hints in ALLERGY_FAMILY_HINTS.items():
        if any(h in s for h in hints):
            return family
    return None


def allergy_conflicts(text: str, allergies: list[dict]) -> list[dict]:
    """Drugs mentioned in the text that the patient is allergic to, directly or by family.

    Sentences that talk *about* the allergy ("allergic to penicillin", "avoid ibuprofen",
    "stopped amoxicillin") aren't flagged; a plain mention of the drug is.
    """
    out, seen = [], set()
    for sentence in re.split(r"(?<=[.;\n])\s*", text):
        if not sentence.strip() or NEGATION.search(sentence):
            continue
        low = sentence.lower()
        for allergy in allergies:
            family = allergy_family(allergy["substance"])
            drugs = list(DRUG_FAMILIES.get(family, ()))
            if allergy.get("category") == "medication" or family:
                drugs.append(allergy["substance"].lower())
            for drug in drugs:
                m = re.search(rf"\b{re.escape(drug)}\w*", low)
                if m and (allergy["substance"], m.group(0)) not in seen:
                    seen.add((allergy["substance"], m.group(0)))
                    quote = sentence[m.start():m.end()]
                    same = drug == allergy["substance"].lower()
                    why = "" if same else f" ({quote} is in the {family} family)"
                    out.append(finding("allergy_conflict",
                                       f"The chart lists an allergy to {allergy['substance']}, and the note "
                                       f"mentions {quote}{why}.", quote))
    return out


def follow_up_days(note: str) -> list[tuple[int, str]]:
    found = []
    for m in FOLLOW_UP.finditer(note):
        # "return if not better in a week" or "come back in 10 days as needed" is advice, not a visit to book;
        # "follow up in 10 days to see if improving" is a visit.
        if re.search(r"\b(if|unless|as needed|when|prn)\b", m.group(0), re.I) or re.match(
                r"[\s,]*(if|unless|as needed|when|prn)\b", note[m.end():], re.I):
            continue
        word = m.group(1).lower()
        count = int(word) if word.isdigit() else WORDS[word]
        found.append((count * UNIT_DAYS[m.group(2).lower()[0]], m.group(0)))
    return found


def rule_findings(note: str, ctx: Context) -> list[dict]:
    text = note.strip()
    out: list[dict] = []
    if len(text) < 15:
        out.append(finding("too_short", "The note is very short. Is it complete?", severity="info"))
    out += _impossible(text, ctx.entered_vitals)
    out += allergy_conflicts(text, ctx.allergies)
    for days, quote in follow_up_days(text):
        if days > 28:
            continue  # beyond the bookable horizon; nothing to check yet
        if not any(0 < d <= days + 7 for d in ctx.booked_in_days):
            out.append(finding("follow_up_not_booked", f"The note asks for a follow-up ({quote.strip()}), but "
                               "nothing is booked with you in that time.", quote.strip(), severity="info"))
    return out


# ---------------------------------------------------------------------------- the LLM layer
SYSTEM_PROMPT = """You review a doctor's visit note for documentation errors before it is saved.
You do not diagnose, suggest treatment, or judge clinical choices (whether a reading counts as a fever,
whether a drug is the right one). Report only problems you can point to in the note's exact words:
- "contradiction": the note contradicts itself (left vs right, increase vs decrease the same drug,
  two different values for the same reading, "no fever" and "febrile", "non-smoker" and "smokes").
- "chart_mismatch": the note conflicts with something the chart below lists (continuing a medication the
  chart lists as stopped, "no allergies" when the chart lists one). A chart list that says "none" is not a
  mismatch: the chart may simply be incomplete.
- "missing_plan": a diagnosis or assessment is written, and nothing anywhere in the note says what to do:
  no medicine, test, referral, advice (rest, fluids, ice), follow-up or "continue". Any of these is a plan.
Reply with JSON only, in this shape:
{"issues": [{"type": "contradiction", "quote": "exact words from the note", "explanation": "one short sentence"}]}
"quote" must be copied exactly from the note, a few words long. If the note is fine, reply {"issues": []}.
Most notes are fine. Report at most 4 issues, the most important first."""


def chart_text(ctx: Context) -> str:
    allergies = ", ".join(a["substance"] + (f" ({a['reaction']})" if a.get("reaction") else "")
                          for a in ctx.allergies) or "none recorded"
    active = ", ".join(m["name"] for m in ctx.medications if m["status"] == "active") or "none"
    stopped = ", ".join(m["name"] for m in ctx.medications if m["status"] != "active") or "none"
    lines = [f"Allergies: {allergies}", f"Current medications: {active}", f"Stopped medications: {stopped}",
             f"Ongoing conditions: {', '.join(ctx.conditions) or 'none'}"]
    if ctx.last_vitals:
        v = ctx.last_vitals
        lines.append(f"Last recorded vitals ({v.get('label', 'earlier')}): " + _vitals_line(v))
    if ctx.entered_vitals:
        lines.append("Vitals entered for this visit: " + _vitals_line(ctx.entered_vitals))
    return "\n".join(lines)


def _vitals_line(v: dict) -> str:
    parts = []
    if v.get("systolic") and v.get("diastolic"):
        parts.append(f"BP {v['systolic']:g}/{v['diastolic']:g}")
    for key, label in (("heart_rate", "HR"), ("temperature_c", "temp °C"), ("weight_kg", "weight kg")):
        if v.get(key):
            parts.append(f"{label} {v[key]:g}")
    return ", ".join(parts) or "none"


# Words that mean the note says what to do next. Used to drop "missing plan" flags on notes that have one.
PLAN_WORDS = re.compile(
    r"\b(start(ed|ing)?|continue[sd]?|increase[sd]?|decrease[sd]?|stop(ped)?|prescri\w*|refer(red|ral)?|review|"
    r"recheck|follow[- ]?up|f/u|return|rest|fluids|advis\w*|counsel\w*|cream|drops|ointment|x-?ray|scan|"
    r"repeat|order(ed)?|arrang\w*|monitor\w*|schedul\w*|book(ed)?|ice|physio\w*|plan:|discussed|reassur\w*|"
    r"no treatment|(once|twice|three times) (a day|daily))\b|"
    r"\d+(\.\d+)?\s*(mg|mcg|ml|%)",
    re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def ai_findings(gateway: Gateway, note: str, ctx: Context, timeout_s: float) -> tuple[list[dict], dict]:
    """The model's issues that survive validation, and what happened (model, time, how many were dropped)."""
    data, meta = gateway.ask_json(SYSTEM_PROMPT, f"Patient chart:\n{chart_text(ctx)}\n\nVisit note:\n\"\"\"\n"
                                  f"{note.strip()}\n\"\"\"", timeout_s, max_tokens=500)
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise GatewayUnavailable("the reply had no issues list")
    note_norm = _norm(note)
    kept, dropped = [], 0
    for issue in issues[:MAX_AI_ISSUES + 2]:
        if not isinstance(issue, dict):
            dropped += 1
            continue
        kind, quote = issue.get("type"), str(issue.get("quote") or "").strip().strip('"')
        explanation = str(issue.get("explanation") or "").strip()
        # The quote must really be in the note: no flags on words the doctor didn't write.
        if kind not in AI_TYPES or not quote or _norm(quote) not in note_norm or not explanation:
            dropped += 1
            continue
        # Two checks in code, so they hold whatever the model does:
        # a note with any plan word has a plan; an empty chart has nothing to conflict with.
        if (kind == "missing_plan" and PLAN_WORDS.search(note)) or (
                kind == "chart_mismatch" and not (ctx.allergies or ctx.medications or ctx.conditions)):
            dropped += 1
            continue
        kept.append(finding(kind, explanation[:300], quote[:200], source="ai"))
    return kept[:MAX_AI_ISSUES], {**meta, "dropped": dropped}


def _overlaps(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = _norm(a), _norm(b)
    return a in b or b in a


def check(note: str, ctx: Context, gateway: Gateway | None = None, timeout_s: float = 15.0) -> dict:
    rules = rule_findings(note, ctx)
    ai: dict = {"used": False}
    findings = list(rules)
    if gateway is not None and gateway.enabled and len(note.strip()) >= 15:
        try:
            extra, meta = ai_findings(gateway, note, ctx, timeout_s)
            ai = {"used": True, **meta}
            # Rules are certain; an AI finding on the same words adds nothing.
            findings += [f for f in extra if not any(_overlaps(f["quote"], r["quote"]) for r in rules)]
        except GatewayUnavailable as exc:
            ai = {"used": False, "error": str(exc)}
    order = {"warning": 0, "info": 1}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"findings": findings, "ai": ai}


# ---------------------------------------------------------------------------- from the database
def context_for(session, provider_id: int, appointment_id: int, entered_vitals: dict | None = None) -> Context:
    appt = session.get(Appointment, appointment_id)
    if appt is None or appt.slot.provider_id != provider_id:
        raise NotFound(f"appointment {appointment_id} not found")
    pid, visit_start = appt.patient_id, appt.slot.start
    allergies = session.scalars(select(Allergy).where(Allergy.patient_id == pid, Allergy.status == "active")).all()
    meds = session.scalars(select(Medication).where(Medication.patient_id == pid)
                           .order_by(Medication.status, Medication.started.desc())).all()
    conditions = session.scalars(select(Condition.name).where(Condition.patient_id == pid,
                                                              Condition.status == "active")).all()
    last = session.scalar(select(Vitals).where(Vitals.patient_id == pid, Vitals.appointment_id != appointment_id,
                                               Vitals.taken_at < visit_start).order_by(Vitals.taken_at.desc()))
    future = session.scalars(select(Slot.start).join(Appointment, Appointment.slot_id == Slot.id).where(
        Appointment.patient_id == pid, Slot.provider_id == provider_id, Appointment.status.in_(("booked", "held")),
        Slot.start > visit_start, Slot.start < visit_start + timedelta(days=60))).all()
    stopped = [m for m in meds if m.status != "active"][:6]
    return Context(
        allergies=[{"substance": a.substance, "category": a.category, "reaction": a.reaction} for a in allergies],
        medications=[{"name": m.name, "status": m.status} for m in meds if m.status == "active"]
        + [{"name": m.name, "status": m.status} for m in stopped],
        conditions=list(conditions),
        last_vitals=None if last is None else {
            "label": f"{last.taken_at:%b} {last.taken_at.day}, {last.taken_at.year}", "systolic": last.systolic,
            "diastolic": last.diastolic, "heart_rate": last.heart_rate, "temperature_c": last.temperature_c,
            "weight_kg": last.weight_kg},
        entered_vitals={k: float(v) for k, v in (entered_vitals or {}).items() if v not in (None, "")},
        booked_in_days=[max(1, (start.date() - visit_start.date()).days) for start in future],
    )
