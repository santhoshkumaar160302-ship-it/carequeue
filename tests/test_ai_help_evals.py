"""The scoring in scripts/eval_notes.py and scripts/eval_summary.py, with fake gateways."""
import importlib.util
import json
import sys
from pathlib import Path

import httpx

from carequeue.llm import Gateway

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


notes = load_script("eval_notes")
summ = load_script("eval_summary")
CASES = notes.load(ROOT / "evals" / "note_checks.jsonl")


def gateway(reply_for):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"content": json.dumps(reply_for(body["messages"][1]["content"])),
                                         "model": "fake", "cost_usd": 0.0001})
    return Gateway("http://nexus.test", transport=httpx.MockTransport(handler))


def test_the_labelled_notes_are_well_formed():
    assert len(CASES) >= 60 and len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert (c["category"] == "clean") == (c["expected"] == [])
        for exp in c["expected"]:
            for quote in (exp["quote"], *exp.get("also", [])):
                assert quote.lower() in c["note"].lower(), c["id"]  # the planted mistake is really in the note


def test_rules_alone_catch_their_categories_without_false_alarms():
    r = notes.score(CASES, None, 5)
    assert r["recall_rule_categories"] == 1.0 and r["recall_ai_categories"] == 0.0
    assert r["false_alarms"] == 0 and r["clean_notes_flagged"] == 0


def test_a_perfect_ai_scores_full_recall_and_a_noisy_one_scores_false_alarms():
    by_note = {c["note"]: c for c in CASES}

    def perfect(prompt):
        case = next(c for note, c in by_note.items() if note in prompt)
        issues = [{"type": case["category"], "quote": e["quote"], "explanation": "planted"}
                  for e in case["expected"] if case["category"] in notes.AI_CATEGORIES]
        return {"issues": issues}
    r = notes.score(CASES, gateway(perfect), 5)
    # Known trade-off: in a note with two problems, a plan for one ("continue lisinopril") hides a missing plan
    # for the other, because the guard drops "missing plan" on any note that has a plan word.
    assert [m["id"] for m in r["misses"]] == [60]
    assert r["false_alarms"] == 0 and r["ai_unavailable"] == 0

    def noisy(prompt):  # flags the first few words of every note
        note = next(n for n in by_note if n in prompt)
        return {"issues": [{"type": "contradiction", "quote": " ".join(note.split()[:3]), "explanation": "?"}]}
    r = notes.score(CASES, gateway(noisy), 5)
    assert r["clean_notes_flagged"] == r["clean_notes"] and r["false_alarms"] > 0


def test_tamper_changes_a_number_its_sources_do_not_have():
    assert summ.tamper("BP 138/88 on Sep 8.", "Sep 8, 2026: BP 138/88") == "BP 145/88 on Sep 8."
    assert summ.tamper("Allergic to penicillin.", "Penicillin") is None
    assert summ.tamper("2 missed visits.", "") is None  # small counts are left alone


def test_summary_eval_counts_kept_dropped_and_tampered_lines():
    Session = summ.demo_session()
    picks = summ.cases(Session, 6)
    assert len({p[0] for p in picks}) > 1  # spread across doctors

    def reply(prompt):
        first = prompt.split("[", 1)[1].split("]", 1)[0]
        vitals = next((ln for ln in prompt.splitlines() if "(vitals)" in ln and "BP" in ln), None)
        lines = [{"text": "See the record.", "sources": [first]},
                 {"text": "Weight 412 kg.", "sources": [first]}]  # made up: always dropped
        if vitals:
            vid = vitals.split("]")[0].lstrip("[")
            bp = vitals.split("BP ")[1].split(",")[0]
            lines.append({"text": f"Last BP {bp}.", "sources": [vid]})
        return {"lines": lines}
    r = summ.run(Session, picks, gateway(reply), 5, progress=False)
    assert r["kind"] == {"ai": 6} and r["lines_dropped"] == 6
    assert r["dropped_by_reason"] == {"made-up numbers": 6}
    caught, total = map(int, r["tamper_test"].split("/"))
    assert total > 0 and caught == total
    assert r["cost_usd_total"] > 0
