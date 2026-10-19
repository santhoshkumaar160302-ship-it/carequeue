"""The assistant accuracy test: the labeled set is well-formed and the scoring is right."""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_assistant", ROOT / "scripts" / "eval_assistant.py")
ev = importlib.util.module_from_spec(spec)
sys.modules["eval_assistant"] = ev  # dataclasses need the module registered
spec.loader.exec_module(ev)
CASES = ev.load_cases(ROOT / "evals" / "assistant_requests.jsonl")


def test_labels_are_well_formed():
    names = {p["name"] for p in ev.PROVIDER_LIST}
    specialties = {p["specialty"] for p in ev.PROVIDER_LIST}
    assert len(CASES) >= 50 and len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert (c["provider"] is None) != (c["specialty"] is None), c  # exactly one of the two
        assert c["provider"] in names | {None} and c["specialty"] in specialties | {None}, c
        assert ev.TODAY <= date.fromisoformat(c["earliest"]) <= date.fromisoformat(c["latest"]), c
        assert c["time_of_day"] in ("any", "morning", "afternoon"), c


def perfect_gateway():
    """A fake NexusAI that always replies with the right answer."""
    by_text = {c["text"]: c for c in CASES}

    def handler(request):
        case = by_text[json.loads(request.content)["messages"][-1]["content"]]
        answer = {k: case[k] for k in ("provider", "specialty", "earliest", "latest", "time_of_day")}
        return httpx.Response(200, json={"content": json.dumps(answer), "model": "fake-model", "cache": "miss",
                                         "cost_usd": 0.0001, "latency_ms": 5})
    return httpx.MockTransport(handler)


def test_a_perfect_llm_scores_100_percent():
    scores = ev.run(CASES, "http://nexus.test", None, 5, transport=perfect_gateway())
    llm = scores["llm"].summary()
    assert llm["accuracy"]["all"] == 1.0 and llm["errors"] == 0 and llm["models"] == {"fake-model": len(CASES)}
    assert llm["cost_per_1000_usd"] == 0.1
    assert scores["rules"].correct["all"] < len(CASES)  # the rules miss some, which is the point


def test_when_the_gateway_is_down_the_app_scores_like_the_rules():
    down = httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "down"}))
    scores = ev.run(CASES, "http://nexus.test", None, 5, transport=down)
    assert scores["llm"].correct["all"] == 0 and scores["llm"].errors == len(CASES)
    assert scores["app"].correct == scores["rules"].correct


def test_rules_baseline_has_not_regressed():
    rules = ev.run(CASES, None, None, 5)["rules"]
    assert rules.correct["all"] / rules.total >= 0.55  # 35/60 when this test was written


def test_answers_slower_than_the_app_limit_count_as_rules():
    scores = ev.run(CASES, "http://nexus.test", None, 5, transport=perfect_gateway(), app_timeout_s=0)
    assert scores["llm"].correct["all"] == len(CASES)  # the LLM was right...
    assert scores["app"].late == len(CASES)  # ...but too slow every time,
    assert scores["app"].correct == scores["rules"].correct  # so patients got the rules' answers


def test_the_accuracy_test_bypasses_the_gateway_cache():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(503)
    ev.run(CASES[:2], "http://nexus.test", None, 5, transport=httpx.MockTransport(handler))
    assert all(body["cache"] is False for body in seen)
