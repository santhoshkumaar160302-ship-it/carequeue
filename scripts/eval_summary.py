"""How good and how safe are the AI pre-visit summaries, on the demo clinic's real patients?

For each doctor's patients with a chart and at least one past visit (the same records a
doctor would open), this asks NexusAI for a summary and reports:
- how often the AI answered, and how often the basic summary had to be used instead;
- lines kept, and lines dropped by the source check (and why: made-up numbers, no source);
- allergy coverage: of patients with an active allergy, how many summaries cite it;
- a tamper test: every kept line is copied with one number changed (138/88 -> 145/88),
  and the source check must reject the copy. This measures the guard itself;
- time and cost per summary.

Examples:
  python scripts/eval_summary.py --nexusai-url https://nexusai-latest.onrender.com
  python scripts/eval_summary.py --nexusai-url https://nexusai-latest.onrender.com --limit 10 --show-dropped
  python scripts/eval_summary.py --basic-only      # no AI: checks the fallback summary and the tamper test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from carequeue.app import build_services  # noqa: E402
from carequeue.clock import FakeClock  # noqa: E402
from carequeue.config import Settings  # noqa: E402
from carequeue.llm import Gateway  # noqa: E402
from carequeue.models import Allergy, Appointment, Condition, Medication, Provider, Slot  # noqa: E402
from carequeue.summary import NUMBER, check_line, gather, summarize  # noqa: E402

NOW = datetime(2026, 10, 14, 11, 0)  # a Wednesday morning, as in the tests


def demo_session():
    settings = Settings(database_url="sqlite://", run_background_worker=False, seed_demo=True)
    return build_services(settings, clock=FakeClock(NOW)).clinic.Session


def cases(Session, limit: int) -> list[tuple[int, str, int]]:
    """(provider id, provider name, patient id) for patients with a chart and a past visit with that doctor."""
    out = []
    with Session() as s:
        charted = set(s.scalars(select(Allergy.patient_id))) | set(s.scalars(select(Condition.patient_id))) | set(
            s.scalars(select(Medication.patient_id)))
        for prov in s.scalars(select(Provider).order_by(Provider.id)):
            seen = s.scalars(select(Appointment.patient_id).join(Slot, Appointment.slot_id == Slot.id).where(
                Slot.provider_id == prov.id, Appointment.status == "completed", Slot.start <= NOW)
                .distinct().order_by(Appointment.patient_id))
            out += [(prov.id, prov.name, pid) for pid in seen if pid in charted]
    # Spread the limit across doctors rather than taking the first doctor's patients only.
    rank, seen_per_doctor = {}, Counter()
    for c in out:
        rank[c] = seen_per_doctor[c[0]]
        seen_per_doctor[c[0]] += 1
    return sorted(out, key=lambda c: (rank[c], c[0]))[:limit]


def tamper(text: str, sources_text: str) -> str | None:
    """The line with one number changed to a value its sources don't contain, or None if it has no number."""
    for m in NUMBER.finditer(text):
        n = m.group(0)
        if "." in n or int(n) <= 10:
            continue  # small counts are allowed without a source; dates like 2026 are fine to change
        for delta in (7, 13, 29):
            new = str(int(n) + delta)
            if new not in sources_text:
                return text[:m.start()] + new + text[m.end():]
    return None


def run(Session, picks, gateway: Gateway | None, timeout_s: float, progress: bool) -> dict:
    kinds, reasons = Counter(), Counter()
    kept_lines, dropped_lines, latencies, costs, errors, dropped_examples = 0, 0, [], [], Counter(), []
    with_allergy, allergy_cited = 0, 0
    tampered, tamper_caught = 0, 0
    per_summary_lines = []
    for i, (prov_id, _name, pid) in enumerate(picks, 1):
        if progress:
            print(f"\r  summarizing {i}/{len(picks)}", end="", file=sys.stderr, flush=True)
        with Session() as s:
            sources, open_items = gather(s, prov_id, pid, NOW)
        start = time.perf_counter()
        out = summarize(sources, open_items, gateway, timeout_s)
        latencies.append((time.perf_counter() - start) * 1000)
        kinds[out["kind"]] += 1
        ai = out["ai"]
        if ai.get("error"):
            errors[re.split(r"[:(]", ai["error"])[0].strip()] += 1
        if ai.get("cost_usd"):
            costs.append(ai["cost_usd"])
        for d in ai.get("dropped", []):
            dropped_lines += 1
            reasons["made-up numbers" if d["why"].startswith("numbers") else d["why"]] += 1
            dropped_examples.append({"patient": pid, "doctor": prov_id, **d})
        if out["kind"] == "ai":
            kept_lines += len(out["lines"])
            per_summary_lines.append(len(out["lines"]))
        if any(src.kind == "allergy" for src in sources):
            with_allergy += 1
            allergy_cited += any(c["id"].startswith("A") for line in out["lines"] for c in line["sources"])
        by_id = {src.id: src for src in sources}
        for line in out["lines"]:
            cited_text = " ".join(c["text"] for c in line["sources"])
            bad = tamper(line["text"], cited_text)
            if bad:
                tampered += 1
                ok, _why = check_line({"text": bad, "sources": [c["id"] for c in line["sources"]]}, by_id)
                tamper_caught += ok is None
    if progress:
        print(file=sys.stderr)
    lat = sorted(latencies)
    return {
        "summaries": len(picks), "kind": dict(kinds), "ai_errors": dict(errors),
        "lines_kept": kept_lines, "lines_dropped": dropped_lines, "dropped_by_reason": dict(reasons),
        "lines_per_ai_summary": round(statistics.fmean(per_summary_lines), 1) if per_summary_lines else 0,
        "allergy_coverage": f"{allergy_cited}/{with_allergy}",
        "tamper_test": f"{tamper_caught}/{tampered}",
        "latency_ms": {"mean": round(statistics.fmean(lat)), "p95": round(lat[int(0.95 * (len(lat) - 1))]),
                       "max": round(lat[-1])} if lat
        else {},
        "cost_usd_total": round(sum(costs), 6), "dropped_examples": dropped_examples,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nexusai-url", default=os.getenv("NEXUSAI_URL"))
    ap.add_argument("--api-key", default=os.getenv("NEXUSAI_API_KEY"))
    ap.add_argument("--basic-only", action="store_true", help="skip the AI; test the fallback and the guard")
    ap.add_argument("--limit", type=int, default=30, help="how many summaries (default 30)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--show-dropped", action="store_true", help="print every line the source check dropped")
    ap.add_argument("--out", default=str(ROOT / "results" / "summary_eval.json"))
    args = ap.parse_args()

    url = None if args.basic_only else (args.nexusai_url or "").rstrip("/") or None
    if not url and not args.basic_only:
        print("No NexusAI URL: pass --nexusai-url (or set NEXUSAI_URL), or use --basic-only.")
        return 2
    Session = demo_session()
    picks = cases(Session, args.limit)
    gateway = Gateway(url, args.api_key, use_cache=False) if url else None
    r = run(Session, picks, gateway, args.timeout, progress=bool(url))

    print(f"\n{r['summaries']} summaries across {len({p[0] for p in picks})} doctors"
          + (f", gateway = {url}" if url else " (basic only)"))
    rows = [("AI summaries", r["kind"].get("ai", 0)), ("Basic (fallback) summaries", r["kind"].get("basic", 0)),
            ("Why the AI wasn't used", ", ".join(f"{k} x{v}" for k, v in r["ai_errors"].items()) or "-"),
            ("Lines kept", r["lines_kept"]), ("Lines per AI summary", r["lines_per_ai_summary"]),
            ("Lines dropped by the source check", r["lines_dropped"]),
            ("  why", ", ".join(f"{k} x{v}" for k, v in r["dropped_by_reason"].items()) or "-"),
            ("Allergy cited (when there is one)", r["allergy_coverage"]),
            ("Tamper test (changed numbers caught)", r["tamper_test"]),
            ("Mean time per summary", f"{r['latency_ms'].get('mean', 0)} ms"),
            ("p95 time per summary", f"{r['latency_ms'].get('p95', 0)} ms"),
            ("Slowest summary", f"{r['latency_ms'].get('max', 0)} ms"),
            ("Total cost", f"${r['cost_usd_total']:.4f}")]
    for label, value in rows:
        print(f"{label:<40}{value}")
    if args.show_dropped:
        print("\n--- dropped lines")
        for d in r["dropped_examples"]:
            print(f"patient {d['patient']} / doctor {d['doctor']}: {d['text']!r} -> {d['why']}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"gateway": url, "now": NOW.isoformat(), "results": r}, indent=2) + "\n")
    print(f"\nSaved to {args.out}")
    caught, total = map(int, r["tamper_test"].split("/"))
    return 1 if caught < total else 0


if __name__ == "__main__":
    raise SystemExit(main())
