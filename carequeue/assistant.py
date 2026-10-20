"""Natural-language booking: turn "a skin doctor next Tuesday morning" into a search.

When NEXUSAI_URL is set, the request goes through the NexusAI gateway, which picks
the model, retries and fails over. The model only *extracts* what the patient asked
for as JSON; it never books anything. Its output is validated, and the search runs
against real availability, so a wrong or made-up answer can't create a bad booking.
If the gateway is unreachable or returns something invalid, a rule-based parser takes
over, so booking keeps working without the LLM.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import date, timedelta

import httpx

from .clinic import TIMES_OF_DAY, Intent

log = logging.getLogger("carequeue.assistant")

SPECIALTY_WORDS = {
    "Dermatology": ("skin", "rash", "acne", "mole", "derm", "eczema"),
    "Pediatrics": ("child", "kid", "son", "daughter", "baby", "toddler", "pediatric", "infant"),
    "Cardiology": ("heart", "chest", "cardio", "blood pressure", "palpitation"),
    "General Practice": ("checkup", "check-up", "physical", "general", "flu", "cold", "fever", "cough", "gp",
                         "primary"),
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

SYSTEM_PROMPT = """You extract appointment requests for a clinic. Reply with JSON only, no prose.
Keys:
- "provider": exact provider name from the list, or null
- "specialty": one of the specialties in the list, or null
- "earliest": first acceptable date, YYYY-MM-DD
- "latest": last acceptable date, YYYY-MM-DD
- "time_of_day": "morning", "afternoon", "evening", "night" or "any"
Rules:
- Today is {today} ({weekday}). Each doctor sets their own hours; most work Monday to Friday.
- "Next week" means Monday to Friday of next week; "this week" means today to this Friday.
- A weekday on its own ("Tuesday") is the coming one, today included; "next Tuesday" is the Tuesday of next week.
- "Before Friday" ends the day before; "X or Y" covers both days; "in the next N days" ends N days from today.
- If no date is given, use today to 14 days from today.
- "morning" is 6 AM to noon; "afternoon", "after lunch", "after work" and "after school" mean "afternoon"
  (noon to 6 PM); "evening" and "tonight" are 6 to 10 PM; "night", "late night" and "overnight" are 10 PM to 6 AM.
- Words about when symptoms happened ("woke up at night", "since last night") are not the time of the visit.
- Pick the specialty from the problem: children and babies are Pediatrics even for a rash or a physical;
  skin is Dermatology; heart, chest and blood pressure are Cardiology; anything else is General Practice.
- Only set "provider" when the patient names a doctor; then leave "specialty" null.
Providers: {providers}"""


def _weekday_date(today: date, idx: int, next_week: bool) -> date:
    """"Tuesday" is the coming Tuesday (today if it's Tuesday); "next Tuesday" is in next week."""
    if next_week:
        return _monday(today) + timedelta(days=7 + idx)
    return today + timedelta(days=(idx - today.weekday()) % 7)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def rule_parse(text: str, today: date, providers: list[dict]) -> Intent:
    """Keyword-based fallback. Handles the common phrasings; the LLM handles the rest."""
    t = text.lower()
    intent = Intent()
    for p in providers:
        last = p["name"].split()[-1].lower()
        if last in t or p["name"].lower() in t:
            intent.provider_id = p["id"]
            break
    if intent.provider_id is None:
        for specialty, words in SPECIALTY_WORDS.items():
            if any(re.search(rf"\b{re.escape(w)}", t) for w in words) or specialty.lower() in t:
                intent.specialty = specialty
                break

    # "last night" / "at night" describe symptoms, not the visit, so drop them before looking.
    visit = re.sub(r"\b(last|at|every|during the|all) night\b", " ", t)
    if "morning" in visit or "before noon" in visit or re.search(r"\b(6|7|8|9|10|11)\s*am\b", visit):
        intent.time_of_day = "morning"
    elif "afternoon" in visit or "after lunch" in visit or re.search(r"\b(12|1|2|3|4|5)\s*pm\b", visit):
        intent.time_of_day = "afternoon"
    elif re.search(r"\b(late night|overnight|night shift|after midnight|night)\b", visit) or re.search(
            r"\b(10|11)\s*pm\b|\b(12|1|2|3|4|5)\s*am\b", visit):
        intent.time_of_day = "night"
    elif re.search(r"\b(evening|tonight)\b", visit) or re.search(r"\b(6|7|8|9)\s*pm\b", visit):
        intent.time_of_day = "evening"

    if "today" in t or "tonight" in t:
        intent.earliest = intent.latest = today
    elif "tomorrow" in t:
        intent.earliest = intent.latest = today + timedelta(days=1)
    elif "next week" in t:
        intent.earliest = _monday(today) + timedelta(days=7)
        intent.latest = intent.earliest + timedelta(days=4)
    elif "this week" in t:
        intent.earliest, intent.latest = today, _monday(today) + timedelta(days=4)
    else:
        for i, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name}\b", t):
                day = _weekday_date(today, i, next_week=f"next {name}" in t)
                intent.earliest = intent.latest = day
                break
        iso = re.search(r"\b(20\d\d-\d\d-\d\d)\b", t)
        if iso:
            intent.earliest = intent.latest = date.fromisoformat(iso.group(1))
    if intent.earliest is None:
        intent.earliest, intent.latest = today, today + timedelta(days=14)
    return intent


def _extract_json(content: str) -> dict:
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("no JSON object in the reply")
    return json.loads(match.group(0))


def validate_llm_intent(data: dict, today: date, providers: list[dict]) -> Intent:
    """Accept only values that make sense: known providers, real dates in a sane window."""
    intent = Intent()
    names = {p["name"].lower(): p["id"] for p in providers}
    specialties = {p["specialty"].lower(): p["specialty"] for p in providers}
    if data.get("provider"):
        intent.provider_id = names.get(str(data["provider"]).lower())
        if intent.provider_id is None:
            raise ValueError(f"unknown provider {data['provider']!r}")
    if data.get("specialty") and not intent.provider_id:
        intent.specialty = specialties.get(str(data["specialty"]).lower())
        if intent.specialty is None:
            raise ValueError(f"unknown specialty {data['specialty']!r}")
    tod = (data.get("time_of_day") or "any").lower()
    intent.time_of_day = tod if tod in TIMES_OF_DAY else "any"
    earliest = date.fromisoformat(data["earliest"]) if data.get("earliest") else today
    latest = date.fromisoformat(data["latest"]) if data.get("latest") else earliest + timedelta(days=14)
    if latest < earliest:
        earliest, latest = latest, earliest
    if latest < today or earliest > today + timedelta(days=90):
        raise ValueError("dates outside the bookable window")
    intent.earliest, intent.latest = max(earliest, today), latest
    return intent


def named_in(text: str, provider_name: str) -> bool:
    """Did the patient mention this doctor? Their first or last name counts ("Patel", "Dr Maya")."""
    t = text.lower()
    parts = [w for w in provider_name.lower().replace("dr.", "").split() if len(w) > 2]
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in parts)


def keep_unnamed_doctors_open(intent: Intent, text: str, providers: list[dict]) -> Intent:
    """If the model picked a doctor the patient never named, search the whole specialty instead.

    "My son has a fever" means any pediatrician; narrowing it to one doctor would hide
    other doctors' open times. Checked against the patient's own words, so it doesn't
    depend on the model following the prompt.
    """
    if intent.provider_id is None:
        return intent
    provider = next(p for p in providers if p["id"] == intent.provider_id)
    if not named_in(text, provider["name"]):
        intent.provider_id, intent.specialty = None, provider["specialty"]
    return intent


class Assistant:
    def __init__(self, clinic, nexusai_url: str | None, api_key: str | None = None, timeout_s: float = 3.0,
                 transport: httpx.BaseTransport | None = None):
        self.clinic = clinic
        self.url = nexusai_url
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.transport = transport
        self.use_cache = True  # the accuracy test turns this off, so every answer is fresh from a model

    def _ask_gateway(self, text: str, today: date, providers: list[dict]) -> tuple[Intent, dict]:
        listing = "; ".join(f"{p['name']} ({p['specialty']})" for p in providers)
        system = SYSTEM_PROMPT.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()].title(),
                                      providers=listing)
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                   "temperature": 0, "max_tokens": 300}
        if not self.use_cache:
            payload["cache"] = False
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
            resp = client.post(f"{self.url}/v1/chat", headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
        intent = validate_llm_intent(_extract_json(body.get("content", "")), today, providers)
        intent = keep_unnamed_doctors_open(intent, text, providers)
        return intent, {"model": body.get("model"), "cache": body.get("cache"), "cost_usd": body.get("cost_usd"),
                        "latency_ms": body.get("latency_ms")}

    def suggest(self, text: str, limit: int = 5) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Tell me what you're looking for.")
        if len(text) > 500:
            raise ValueError("Please keep the request under 500 characters.")
        today = self.clinic.clock.now().date()
        providers = self.clinic.providers()
        parser, gateway, fallback_reason = "rules", None, None
        if self.url:
            try:
                intent, gateway = self._ask_gateway(text, today, providers)
                parser = "nexusai"
            except Exception as exc:  # noqa: BLE001 - any gateway problem falls back to the rules
                fallback_reason = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.warning("NexusAI parse failed, using rules: %s", fallback_reason)
                intent = rule_parse(text, today, providers)
        else:
            intent = rule_parse(text, today, providers)
        options = self.clinic.open_slots(intent, limit=limit)
        names = {p["id"]: p["name"] for p in providers}
        understood = asdict(intent)
        understood["provider"] = names.get(intent.provider_id)
        for key in ("earliest", "latest"):
            understood[key] = understood[key].isoformat() if understood[key] else None
        return {"parser": parser, "gateway": gateway, "fallback_reason": fallback_reason,
                "understood": understood, "options": options}
