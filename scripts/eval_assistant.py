"""How well does the "Find a time" assistant understand patients? A labeled accuracy test.

Every request in evals/assistant_requests.jsonl has the correct answer: which doctor or
specialty, which dates and which time of day. This script runs each request through
  - the keyword rules (always, no network), and
  - the LLM through NexusAI (when a gateway URL is given),
then scores each field and the whole answer.

"Today" is fixed at the date in the test set, so the labels never go stale.

Examples:
  python scripts/eval_assistant.py                                   # rules only
  python scripts/eval_assistant.py --nexusai-url https://nexusai-latest.onrender.com
  python scripts/eval_assistant.py --show-failures                   # print every miss
  python scripts/eval_assistant.py --min-accuracy 0.4                # fail (exit 1) below 40% (used by CI)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from carequeue.assistant import Assistant, rule_parse  # noqa: E402
from carequeue.clinic import Intent  # noqa: E402
from carequeue.seed import PROVIDERS  # noqa: E402

TODAY = date(2026, 10, 15)  # a Thursday; every label in the test set is relative to this day
FIELDS = ("doctor", "earliest", "latest", "time_of_day")
PROVIDER_LIST = [{"id": i, "name": name, "specialty": specialty} for i, (name, specialty) in enumerate(PROVIDERS, 1)]


def load_cases(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def field_values(intent: Intent) -> dict:
    names = {p["id"]: p["name"] for p in PROVIDER_LIST}
    doctor = names.get(intent.provider_id) if intent.provider_id else intent.specialty
    return {"doctor": doctor, "earliest": intent.earliest.isoformat() if intent.earliest else None,
            "latest": intent.latest.isoformat() if intent.latest else None, "time_of_day": intent.time_of_day}


def expected_values(case: dict) -> dict:
    return {"doctor": case["provider"] or case["specialty"], "earliest": case["earliest"],
            "latest": case["latest"], "time_of_day": case["time_of_day"]}


@dataclass
class Score:
    name: str
    correct: dict = field(default_factory=lambda: {f: 0 for f in (*FIELDS, "all")})
    total: int = 0
    latencies_ms: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    errors: int = 0  # the gateway failed or replied with something invalid
    late: int = 0  # answered, but after the app's time limit, so the app used the rules
    models: dict = field(default_factory=dict)
    cache_hits: int = 0
    cost_usd: float = 0.0

    def add(self, case: dict, got: dict | None, latency_ms: float) -> None:
        self.total += 1
        self.latencies_ms.append(latency_ms)
        want = expected_values(case)
        if got is None:
            self.failures.append({"id": case["id"], "text": case["text"], "wrong": ["no answer"]})
            return
        wrong = [f for f in FIELDS if got[f] != want[f]]
        for f in FIELDS:
            self.correct[f] += f not in wrong
        self.correct["all"] += not wrong
        if wrong:
            self.failures.append({"id": case["id"], "text": case["text"], "wrong": wrong,
                                  "expected": {f: want[f] for f in wrong}, "got": {f: got[f] for f in wrong}})

    def summary(self) -> dict:
        out = {"requests": self.total,
               "accuracy": {f: round(self.correct[f] / self.total, 4) for f in (*FIELDS, "all")},
               "latency_ms": {"mean": round(statistics.fmean(self.latencies_ms), 1),
                              "p95": round(sorted(self.latencies_ms)[int(0.95 * (len(self.latencies_ms) - 1))], 1)}}
        if self.name == "app":
            out["late"] = self.late
        if self.name != "rules":
            out |= {"errors": self.errors, "models": self.models, "cache_hits": self.cache_hits,
                    "cost_usd": round(self.cost_usd, 6),
                    "cost_per_1000_usd": round(self.cost_usd / self.total * 1000, 4)}
        return out


def run(cases: list[dict], nexusai_url: str | None, api_key: str | None, timeout_s: float,
        transport=None, app_timeout_s: float = 3.0, use_cache: bool = False) -> dict[str, Score]:
    scores = {"rules": Score("rules")}
    for case in cases:
        start = time.perf_counter()
        intent = rule_parse(case["text"], TODAY, PROVIDER_LIST)
        scores["rules"].add(case, field_values(intent), (time.perf_counter() - start) * 1000)
    if not nexusai_url:
        return scores

    # app = what patients actually get: the LLM's answer if it arrives within the app's time
    # limit (NEXUSAI_TIMEOUT_S), otherwise the keyword rules
    llm, app = Score("llm"), Score("app")
    assistant = Assistant(None, nexusai_url, api_key, timeout_s, transport=transport)
    assistant.use_cache = use_cache  # off by default: a cached answer would hide the model's real accuracy and speed
    for i, case in enumerate(cases, 1):
        print(f"\r  asking NexusAI {i}/{len(cases)}", end="", file=sys.stderr, flush=True)
        start = time.perf_counter()
        try:
            intent, meta = assistant._ask_gateway(case["text"], TODAY, PROVIDER_LIST)
            got = field_values(intent)
            for s in (llm, app):
                s.models[meta.get("model") or "?"] = s.models.get(meta.get("model") or "?", 0) + 1
                s.cache_hits += meta.get("cache") not in (None, "miss")
                s.cost_usd += meta.get("cost_usd") or 0.0
        except Exception as exc:  # noqa: BLE001 - count it, the app would fall back to the rules
            got = None
            llm.errors += 1
            app.errors += 1
            reason = (str(exc).splitlines() or [""])[0][:120]
            print(f"\n  #{case['id']} gateway error: {type(exc).__name__}: {reason}", file=sys.stderr)
        elapsed = (time.perf_counter() - start) * 1000
        llm.add(case, got, elapsed)
        in_time = got is not None and elapsed <= app_timeout_s * 1000
        if got is not None and not in_time:
            app.late += 1
        app.add(case, got if in_time else field_values(rule_parse(case["text"], TODAY, PROVIDER_LIST)),
                min(elapsed, app_timeout_s * 1000))
    print(file=sys.stderr)
    scores |= {"llm": llm, "app": app}
    return scores


def print_table(scores: dict[str, Score]) -> None:
    labels = {"rules": "Keyword rules", "llm": "LLM via NexusAI", "app": "App (LLM, else rules)"}
    cols = list(scores)
    rows = [("Fully correct", "all"), ("Doctor / specialty", "doctor"), ("First date", "earliest"),
            ("Last date", "latest"), ("Time of day", "time_of_day")]
    width = 30
    print("\n" + "".ljust(22) + "".join(labels[c].rjust(width) for c in cols))
    for label, key in rows:
        print(label.ljust(22) + "".join(
            f"{scores[c].correct[key]}/{scores[c].total} ({scores[c].correct[key] / scores[c].total:.0%})".rjust(width)
            for c in cols))
    print("Mean latency".ljust(22) + "".join(f"{statistics.fmean(scores[c].latencies_ms):.0f} ms".rjust(width)
                                             for c in cols))
    print("p95 latency".ljust(22) + "".join(f"{scores[c].summary()['latency_ms']['p95']:.0f} ms".rjust(width)
                                            for c in cols))
    if "llm" in scores:
        s = scores["llm"].summary()
        late = scores["app"].late
        print(f"\nGateway errors: {s['errors']}   too slow for the app's limit: {late}   cache hits: {s['cache_hits']}"
              f"   models: {s['models']}")
        print(f"Cost: ${s['cost_usd']:.6f} in total, ${s['cost_per_1000_usd']:.4f} per 1,000 requests")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(ROOT / "evals" / "assistant_requests.jsonl"))
    ap.add_argument("--nexusai-url", default=os.getenv("NEXUSAI_URL"), help="defaults to $NEXUSAI_URL")
    ap.add_argument("--api-key", default=os.getenv("NEXUSAI_API_KEY"))
    ap.add_argument("--rules-only", action="store_true", help="skip the LLM even if NEXUSAI_URL is set")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per request (a sleeping gateway is slow)")
    ap.add_argument("--app-timeout", type=float, default=float(os.getenv("NEXUSAI_TIMEOUT_S", "3")),
                    help="the app's limit (NEXUSAI_TIMEOUT_S): slower LLM answers count as falling back to the rules")
    ap.add_argument("--use-cache", action="store_true",
                    help="let NexusAI answer from its cache (off by default, so every answer comes from a model)")
    ap.add_argument("--show-failures", action="store_true")
    ap.add_argument("--min-accuracy", type=float, help="exit 1 if the rules' fully-correct rate is below this")
    ap.add_argument("--out", default=str(ROOT / "results" / "assistant_eval.json"))
    args = ap.parse_args()

    cases = load_cases(Path(args.cases))
    url = None if args.rules_only else (args.nexusai_url or "").rstrip("/") or None
    print(f"{len(cases)} labeled requests, today = {TODAY:%a %b %d, %Y}" + (f", gateway = {url}" if url else ""))
    scores = run(cases, url, args.api_key, args.timeout, app_timeout_s=args.app_timeout,
                 use_cache=args.use_cache)
    print_table(scores)

    if args.show_failures:
        for name, s in scores.items():
            print(f"\n--- {name}: {len(s.failures)} misses")
            for f in s.failures:
                print(f"#{f['id']} {f['text']!r}: " + ", ".join(
                    f"{k} expected {f.get('expected', {}).get(k)!r} got {f.get('got', {}).get(k)!r}" for k in f["wrong"]
                    if k != "no answer") if "expected" in f else f"#{f['id']} {f['text']!r}: no answer")

    report = {"today": TODAY.isoformat(), "cases": len(cases), "gateway": url, "app_timeout_s": args.app_timeout,
              "results": {name: s.summary() for name, s in scores.items()},
              "failures": {name: s.failures for name, s in scores.items()}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nSaved to {args.out}")

    if args.min_accuracy is not None:
        rules = scores["rules"].correct["all"] / scores["rules"].total
        if rules < args.min_accuracy:
            print(f"FAIL: rules got {rules:.0%} fully correct, below the {args.min_accuracy:.0%} floor")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
