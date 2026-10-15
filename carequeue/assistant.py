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
- "time_of_day": "morning", "afternoon" or "any"
Today is {today} ({weekday}). "Next week" means Monday to Friday of next week.
If no date is given, use today to 14 days from today.
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

    if "morning" in t or "before noon" in t or re.search(r"\b(8|9|10|11)\s*am\b", t):
        intent.time_of_day = "morning"
    elif "afternoon" in t or "after lunch" in t or re.search(r"\b(12|1|2|3|4)\s*pm\b", t):
        intent.time_of_day = "afternoon"

    if "today" in t:
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


class Assistant:
    def __init__(self, clinic, nexusai_url: str | None, api_key: str | None = None, timeout_s: float = 25.0,
                 transport: httpx.BaseTransport | None = None):
        self.clinic = clinic
        self.url = nexusai_url
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.transport = transport

    def _ask_gateway(self, text: str, today: date, providers: list[dict]) -> tuple[Intent, dict]:
        listing = "; ".join(f"{p['name']} ({p['specialty']})" for p in providers)
        system = SYSTEM_PROMPT.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()].title(),
                                      providers=listing)
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
            resp = client.post(f"{self.url}/v1/chat", headers=headers, json={
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "temperature": 0, "max_tokens": 300,
            })
        resp.raise_for_status()
        body = resp.json()
        intent = validate_llm_intent(_extract_json(body.get("content", "")), today, providers)
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
