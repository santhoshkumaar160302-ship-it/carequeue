"""How well does the note check find mistakes, and how often does it raise false alarms?

evals/note_checks.jsonl has 68 short visit notes, each with the patient's chart:
- 47 have one planted mistake each, with the words that show it ("expected" quote, and for a
  contradiction the other side too, since quoting either side points at the mistake):
  impossible values, vitals that disagree, allergy conflicts, unbooked follow-ups
  (the rules' job), and contradictions, chart mismatches and missing plans (the AI's job);
- 21 are clean, realistic notes that should raise nothing, including tricky ones
  ("allergic to penicillin, so azithromycin", "take 1/2 tablet").
12 cases are marked "added": "round 2". They were written after the first live run, to check
that the fixes made then hold on notes the fixes weren't tuned on.

A mistake counts as caught when a finding quotes the same words (or overlapping words).
Every finding that doesn't match a planted mistake is a false alarm.

Examples:
  python scripts/eval_notes.py                                   # rules only
  python scripts/eval_notes.py --nexusai-url https://nexusai-latest.onrender.com --show-misses
  python scripts/eval_notes.py --min-rules-recall 0.95           # used by CI on the rules' own categories
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from carequeue.llm import Gateway  # noqa: E402
from carequeue.notecheck import Context, _overlaps, check  # noqa: E402

RULE_CATEGORIES = ("impossible_value", "vitals_mismatch", "allergy_conflict", "follow_up_not_booked")
AI_CATEGORIES = ("contradiction", "chart_mismatch", "missing_plan")


def load(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def context(case: dict) -> Context:
    chart = case["chart"]
    return Context(allergies=chart["allergies"], medications=chart["medications"], conditions=chart["conditions"],
                   entered_vitals=case["entered_vitals"], booked_in_days=case["booked_in_days"])


def score(cases: list[dict], gateway: Gateway | None, timeout_s: float) -> dict:
    caught = defaultdict(int)
    planted = defaultdict(int)
    false_alarms, clean_flagged, latencies, misses, alarms = 0, 0, [], [], []
    ai_errors: dict[str, int] = defaultdict(int)
    round2 = {"planted": 0, "caught": 0, "clean": 0, "clean_flagged": 0}
    for i, case in enumerate(cases, 1):
        if gateway is not None:
            print(f"\r  checking {i}/{len(cases)}", end="", file=sys.stderr, flush=True)
        start = time.perf_counter()
        result = check(case["note"], context(case), gateway, timeout_s)
        latencies.append((time.perf_counter() - start) * 1000)
        if gateway is not None and not result["ai"].get("used"):
            ai_errors[str(result["ai"].get("error", "unknown")).split(":")[0]] += 1
        findings = result["findings"]
        matched = set()
        for exp in case["expected"]:
            planted[case["category"]] += 1
            # A contradiction has two sides; quoting either one counts ("also" lists the other side).
            hit = [j for j, f in enumerate(findings)
                   if any(_overlaps(f["quote"], q) for q in (exp["quote"], *exp.get("also", [])))]
            if case.get("added") == "round 2":
                round2["planted"] += 1
                round2["caught"] += bool(hit)
            if hit:
                caught[case["category"]] += 1
                matched.update(hit)
            else:
                misses.append({"id": case["id"], "category": case["category"], "expected": exp["quote"],
                               "found": [f["message"] for f in findings]})
        extra = [f for j, f in enumerate(findings) if j not in matched]
        false_alarms += len(extra)
        alarms += [{"id": case["id"], "category": case["category"], "source": f["source"], "message": f["message"],
                    "quote": f["quote"]} for f in extra]
        if case["category"] == "clean" and findings:
            clean_flagged += 1
        if case["category"] == "clean" and case.get("added") == "round 2":
            round2["clean"] += 1
            round2["clean_flagged"] += bool(findings)
    if gateway is not None:
        print(file=sys.stderr)
    total = sum(planted.values())

    def recall(cats):
        n = sum(planted[c] for c in cats)
        return round(sum(caught[c] for c in cats) / n, 4) if n else 0.0
    return {
        "notes": len(cases), "planted": total, "caught": sum(caught.values()),
        "recall": round(sum(caught.values()) / total, 4) if total else 0.0,
        "recall_rule_categories": recall(RULE_CATEGORIES), "recall_ai_categories": recall(AI_CATEGORIES),
        "by_category": {c: f"{caught[c]}/{planted[c]}" for c in (*RULE_CATEGORIES, *AI_CATEGORIES)},
        "false_alarms": false_alarms, "clean_notes_flagged": clean_flagged,
        "clean_notes": sum(1 for c in cases if c["category"] == "clean"),
        "ai_unavailable": sum(ai_errors.values()), "ai_errors": dict(ai_errors), "round2": round2,
        "latency_ms": {"mean": round(statistics.fmean(latencies)),
                       "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))])},
        "misses": misses, "alarms": alarms,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(ROOT / "evals" / "note_checks.jsonl"))
    ap.add_argument("--nexusai-url", default=os.getenv("NEXUSAI_URL"))
    ap.add_argument("--api-key", default=os.getenv("NEXUSAI_API_KEY"))
    ap.add_argument("--rules-only", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--show-misses", action="store_true", help="print every missed mistake and every false alarm")
    ap.add_argument("--min-rules-recall", type=float, help="exit 1 if the rules catch less than this of their own "
                                                            "categories, or flag any clean note (used by CI)")
    ap.add_argument("--out", default=str(ROOT / "results" / "note_check_eval.json"))
    args = ap.parse_args()

    cases = load(Path(args.cases))
    results = {"rules": score(cases, None, args.timeout)}
    url = None if args.rules_only else (args.nexusai_url or "").rstrip("/") or None
    if url:
        results["rules+ai"] = score(cases, Gateway(url, args.api_key, use_cache=False), args.timeout)

    print(f"\n{len(cases)} notes, {results['rules']['planted']} planted mistakes, "
          f"{results['rules']['clean_notes']} clean notes" + (f", gateway = {url}" if url else ""))
    cols = list(results)
    print("\n" + "".ljust(34) + "".join(c.rjust(16) for c in cols))
    rows = [("Mistakes caught", lambda r: f"{r['caught']}/{r['planted']} ({r['recall']:.0%})"),
            ("  rule categories", lambda r: f"{r['recall_rule_categories']:.0%}"),
            ("  AI categories", lambda r: f"{r['recall_ai_categories']:.0%}")]
    rows += [(f"    {c.replace('_', ' ')}", lambda r, c=c: r["by_category"][c]) for c in (*RULE_CATEGORIES,
                                                                                          *AI_CATEGORIES)]
    rows += [("False alarms (all notes)", lambda r: str(r["false_alarms"])),
             ("Clean notes flagged", lambda r: f"{r['clean_notes_flagged']}/{r['clean_notes']}"),
             ("Mean time per note", lambda r: f"{r['latency_ms']['mean']} ms"),
             ("p95 time per note", lambda r: f"{r['latency_ms']['p95']} ms")]
    for label, fn in rows:
        print(label.ljust(34) + "".join(fn(results[c]).rjust(16) for c in cols))
    r2 = results[cols[-1]]["round2"]
    if r2["planted"] or r2["clean"]:
        print(f"\nRound 2 cases only (added after the first live run, {cols[-1]}): mistakes caught "
              f"{r2['caught']}/{r2['planted']}, clean notes flagged {r2['clean_flagged']}/{r2['clean']}")
    if url:
        errs = ", ".join(f"{k} x{v}" for k, v in results["rules+ai"]["ai_errors"].items())
        print(f"AI unavailable on {results['rules+ai']['ai_unavailable']} of {len(cases)} notes"
              + (f" ({errs})." if errs else "."))

    if args.show_misses:
        for name, r in results.items():
            print(f"\n--- {name}: missed")
            for m in r["misses"]:
                print(f"#{m['id']} [{m['category']}] expected {m['expected']!r}; found {m['found'] or 'nothing'}")
            print(f"--- {name}: false alarms")
            for a in r["alarms"]:
                print(f"#{a['id']} [{a['category']}] ({a['source']}) {a['message']} {a['quote']!r}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"notes": len(cases), "gateway": url, "results": results}, indent=2) + "\n")
    print(f"\nSaved to {args.out}")

    if args.min_rules_recall is not None:
        r = results["rules"]
        if r["recall_rule_categories"] < args.min_rules_recall or r["clean_notes_flagged"]:
            print(f"FAIL: rules caught {r['recall_rule_categories']:.0%} of their categories "
                  f"(floor {args.min_rules_recall:.0%}) and flagged {r['clean_notes_flagged']} clean notes")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
