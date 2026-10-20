import json
from datetime import date

import httpx
import pytest

from carequeue.assistant import Assistant, rule_parse, validate_llm_intent

TODAY = date(2026, 10, 12)  # Monday
PROVIDERS = [{"id": 1, "name": "Dr. Maya Patel", "specialty": "General Practice"},
             {"id": 2, "name": "Dr. Lena Brooks", "specialty": "Dermatology"}]


@pytest.mark.parametrize("text, expected", [
    ("I need a skin doctor next Tuesday morning",
     {"specialty": "Dermatology", "earliest": date(2026, 10, 20), "latest": date(2026, 10, 20),
      "time_of_day": "morning"}),
    ("checkup with Dr. Patel tomorrow afternoon",
     {"provider_id": 1, "earliest": date(2026, 10, 13), "time_of_day": "afternoon"}),
    ("anything next week for my rash",
     {"specialty": "Dermatology", "earliest": date(2026, 10, 19), "latest": date(2026, 10, 23)}),
    ("Brooks on Thursday", {"provider_id": 2, "earliest": date(2026, 10, 15)}),
    ("fever, as soon as possible", {"specialty": "General Practice", "earliest": TODAY,
                                    "latest": date(2026, 10, 26)}),
    ("appointment on 2026-10-21 at 10 am", {"earliest": date(2026, 10, 21), "time_of_day": "morning"}),
])
def test_rule_parser(text, expected):
    intent = rule_parse(text, TODAY, PROVIDERS)
    for key, value in expected.items():
        assert getattr(intent, key) == value, (key, intent)


def test_llm_output_is_validated():
    good = validate_llm_intent({"provider": "dr. maya patel", "earliest": "2026-10-14", "latest": "2026-10-13",
                                "time_of_day": "MORNING"}, TODAY, PROVIDERS)
    assert good.provider_id == 1 and good.earliest == date(2026, 10, 13) and good.time_of_day == "morning"
    with pytest.raises(ValueError, match="unknown provider"):
        validate_llm_intent({"provider": "Dr. House"}, TODAY, PROVIDERS)
    with pytest.raises(ValueError, match="unknown specialty"):
        validate_llm_intent({"specialty": "Neurosurgery"}, TODAY, PROVIDERS)
    with pytest.raises(ValueError, match="outside"):
        validate_llm_intent({"earliest": "2030-01-01", "latest": "2030-01-02"}, TODAY, PROVIDERS)


def gateway(reply: str | Exception, seen: list):
    def handler(request):
        seen.append(json.loads(request.content))
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"content": reply, "model": "gemini-3.1-flash-lite", "cache": "miss",
                                         "cost_usd": 0.00002, "latency_ms": 420})
    return httpx.MockTransport(handler)


def test_uses_nexusai_when_configured(clinic):
    seen = []
    reply = '```json\n{"provider": null, "specialty": "Dermatology", "earliest": "2026-10-13", ' \
            '"latest": "2026-10-13", "time_of_day": "afternoon"}\n```'
    a = Assistant(clinic, "http://nexus.test", api_key="k", transport=gateway(reply, seen))
    out = a.suggest("something for my skin tomorrow after lunch")
    assert out["parser"] == "nexusai" and out["gateway"]["model"] == "gemini-3.1-flash-lite"
    assert out["options"] and all(o["specialty"] == "Dermatology" and o["start"].startswith("2026-10-13T1")
                                  for o in out["options"])
    assert seen[0]["messages"][0]["role"] == "system" and "Dr. Lena Brooks" in seen[0]["messages"][0]["content"]


@pytest.mark.parametrize("reply", ["Sure! Dermatology on Tuesday.", '{"provider": "Dr. Who"}',
                                   httpx.ConnectTimeout("timed out")])
def test_falls_back_to_rules_when_the_gateway_fails(clinic, reply):
    a = Assistant(clinic, "http://nexus.test", transport=gateway(reply, []))
    out = a.suggest("skin doctor tomorrow")
    assert out["parser"] == "rules" and out["fallback_reason"]
    assert out["understood"]["specialty"] == "Dermatology" and out["options"]


def test_rules_only_without_a_gateway(clinic):
    out = Assistant(clinic, None).suggest("Dr. Patel on Wednesday morning")
    assert out["parser"] == "rules" and out["understood"]["provider"] == "Dr. Maya Patel"
    assert all(o["start"].startswith("2026-10-14T") for o in out["options"])
    with pytest.raises(ValueError):
        Assistant(clinic, None).suggest("   ")


def test_a_doctor_the_patient_never_named_becomes_their_specialty():
    from carequeue.assistant import keep_unnamed_doctors_open, named_in

    picked = validate_llm_intent({"provider": "Dr. Lena Brooks", "earliest": "2026-10-13"}, TODAY, PROVIDERS)
    widened = keep_unnamed_doctors_open(picked, "something for my rash", PROVIDERS)
    assert widened.provider_id is None and widened.specialty == "Dermatology"
    named = validate_llm_intent({"provider": "Dr. Lena Brooks", "earliest": "2026-10-13"}, TODAY, PROVIDERS)
    assert keep_unnamed_doctors_open(named, "can dr brooks see me", PROVIDERS).provider_id == 2
    assert named_in("Lena on Friday?", "Dr. Lena Brooks") and not named_in("dr. b", "Dr. Lena Brooks")


@pytest.mark.parametrize("text, tod", [
    ("a doctor this evening after work at 7pm", "evening"),
    ("can I see Dr. Patel tonight", "evening"),
    ("I work days, need a late night appointment", "night"),
    ("anything around 11pm on Tuesday", "night"),
    ("my kid threw up last night, can we come this morning", "morning"),  # "last night" is the symptom
    ("coughing all night, any time this week is fine", "any"),
])
def test_rule_parser_reads_evenings_and_nights(text, tod):
    assert rule_parse(text, date(2026, 10, 14), []).time_of_day == tod
