"""Discrete-event simulation: does waitlist auto-fill actually help?

Runs the *real* CareQueue code (the same Clinic class the API uses) against a
simulated clinic, twice, with identical patients, requests and cancellations:

  baseline  canceled slots just go back on the open schedule; only new requests can take them
  waitlist  patients whose first open slot is far away also join the waitlist; canceled
            slots are offered to them (accept / decline / no reply), and accepting
            releases their later slot to the next person

Patients book the earliest open slot with their provider on or after the day they want
(15% want to be seen within a day, the rest plan 2-10 days ahead). Demand runs above
capacity, as in a busy specialty clinic with a backlog, and half of cancellations come in the last
48 hours, when few new patients can take a slot at such short notice.

    python scripts/simulate.py                 # 12 weeks, both policies
    python scripts/simulate.py --weeks 8 --seed 3
"""
from __future__ import annotations

import argparse
import heapq
import json
import random
import statistics
import sys
import time as wall
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from carequeue.broker import InMemoryBroker  # noqa: E402
from carequeue.cache import MemoryScheduleCache  # noqa: E402
from carequeue.clinic import Clinic, Conflict, Intent  # noqa: E402
from carequeue.clock import FakeClock  # noqa: E402
from carequeue.config import Settings  # noqa: E402
from carequeue.db import create_schema, make_engine, make_session_factory  # noqa: E402
from carequeue.models import Appointment, Patient, Provider, Slot  # noqa: E402
from carequeue.seed import PROVIDERS, clinic_days, ensure_slots  # noqa: E402

START = datetime(2026, 1, 5, 8, 0)  # a Monday


@dataclass
class Request:
    pid: int
    at: datetime
    provider_id: int
    wants_from: datetime = START  # earliest date that suits the patient (most book a few days out)
    cancel_notice_h: float | None = None  # if set, cancels this many hours before the visit

    joins_waitlist: bool = False
    responses: list = field(default_factory=list)  # pre-drawn reactions to offers


def make_workload(weeks: int, load: float, cancel_rate: float, waitlist_rate: float, seed: int,
                  same_day_share: float = 0.15):
    rng = random.Random(seed)
    days = clinic_days(START.date(), weeks * 7)
    capacity_per_day = len(PROVIDERS) * 16
    requests = []
    for day in days:
        for _ in range(_poisson(rng, capacity_per_day * load)):
            at = datetime.combine(day, time(8)) + timedelta(minutes=rng.randrange(0, 10 * 60))
            r = Request(pid=len(requests) + 1, at=at, provider_id=rng.randrange(1, len(PROVIDERS) + 1))
            # Some want to be seen within a day; most plan 2-10 days ahead.
            lead = rng.uniform(0, 1) if rng.random() < same_day_share else rng.uniform(2, 10)
            r.wants_from = at + timedelta(days=lead)
            if rng.random() < cancel_rate:
                # Half of cancellations come late (2-48 hours before the visit), half earlier.
                r.cancel_notice_h = rng.uniform(2, 48) if rng.random() < 0.5 else rng.uniform(48, 240)
            r.joins_waitlist = rng.random() < waitlist_rate
            # How this patient reacts to offers: accept (after a few minutes), decline, or never reply.
            for _ in range(6):
                u = rng.random()
                r.responses.append(("accept", rng.uniform(1, 12)) if u < 0.7 else
                                   ("decline", rng.uniform(1, 12)) if u < 0.85 else ("ignore", 0))
            requests.append(r)
    return days, requests


def _poisson(rng, lam):
    # Knuth's method is slow for large lambda; a normal approximation is fine here.
    return max(0, int(round(rng.gauss(lam, lam ** 0.5))))


def build(weeks: int):
    engine = make_engine("sqlite://")
    create_schema(engine)
    Session = make_session_factory(engine)
    clock = FakeClock(START)
    settings = Settings(database_url="sqlite://", seed_demo=False, run_background_worker=False,
                        hold_minutes=15, min_notice_minutes=60)
    with Session() as s:
        s.add_all(Provider(name=n, specialty=sp) for n, sp in PROVIDERS)
        s.flush()
        ensure_slots(s, START.date(), weeks * 7 + 70)
        s.commit()
    return Clinic(Session, clock, settings, MemoryScheduleCache(60), InMemoryBroker()), engine


def run(policy: str, weeks: int, requests: list[Request], horizon_days: int = 60) -> dict:
    clinic, engine = build(weeks)
    with clinic.Session() as s:
        s.add_all(Patient(name=f"Patient {r.pid}", phone="555") for r in requests)
        s.commit()
    by_pid = {r.pid: r for r in requests}
    queue: list[tuple[datetime, int, str, int]] = []
    seq = 0

    def push(at, kind, ref):
        nonlocal seq
        seq += 1
        heapq.heappush(queue, (at, seq, kind, ref))

    for r in requests:
        push(r.at, "request", r.pid)
    stats = {"requests": 0, "booked": 0, "unserved": 0, "cancellations": 0, "offers": 0, "accepted": 0,
             "declined": 0, "expired": 0, "events": 0}
    offer_count: dict[int, int] = {}
    seen_holds: set[int] = set()

    def schedule_offer_responses():
        with clinic.Session() as s:
            holds = s.execute(select(Appointment.id, Appointment.patient_id, Appointment.hold_expires_at)
                              .where(Appointment.status == "held")).all()
        for appt_id, pid, expires in holds:
            if appt_id in seen_holds:
                continue
            seen_holds.add(appt_id)
            stats["offers"] += 1
            n = offer_count.get(pid, 0)
            offer_count[pid] = n + 1
            action, minutes = by_pid[pid].responses[min(n, len(by_pid[pid].responses) - 1)]
            if action == "ignore":
                push(expires + timedelta(seconds=1), "expire", appt_id)
            else:
                push(clinic.clock.now() + timedelta(minutes=minutes), action, appt_id)

    t0 = wall.perf_counter()
    while queue:
        at, _, kind, ref = heapq.heappop(queue)
        clinic.clock.set(at)
        stats["events"] += 1
        if kind == "request":
            r = by_pid[ref]
            stats["requests"] += 1
            options = clinic.open_slots(Intent(provider_id=r.provider_id, earliest=r.wants_from.date(),
                                               latest=r.wants_from.date() + timedelta(days=horizon_days)), limit=1)
            if not options:
                stats["unserved"] += 1
                continue
            appt = clinic.book(r.pid, options[0]["slot_id"])
            stats["booked"] += 1
            first = datetime.fromisoformat(appt["start"])
            if r.cancel_notice_h is not None:
                push(max(at + timedelta(minutes=5), first - timedelta(hours=r.cancel_notice_h)), "cancel", r.pid)
            if policy == "waitlist" and r.joins_waitlist and (first.date() - r.wants_from.date()).days >= 1:
                # Booked later than they wanted: ask to be moved up if something opens in between.
                clinic.join_waitlist(r.pid, max(r.wants_from.date(), at.date()), first.date() - timedelta(days=1),
                                     provider_id=r.provider_id, replaces_appointment_id=appt["id"])
        elif kind == "cancel":
            current = [a for a in clinic.appointments(ref) if a["status"] in ("booked", "held")]
            for a in current:
                if datetime.fromisoformat(a["start"]) > at:
                    clinic.cancel(a["id"])
                    stats["cancellations"] += 1
            for w in clinic.waitlist():
                if w["patient_id"] == ref:
                    clinic.leave_waitlist(w["id"])
        elif kind in ("accept", "decline"):
            try:
                (clinic.accept_hold if kind == "accept" else clinic.decline_hold)(ref)
                stats["accepted" if kind == "accept" else "declined"] += 1
            except Conflict:
                pass  # expired or withdrawn meanwhile
        elif kind == "expire":
            stats["expired"] += clinic.expire_holds()
        clinic.run_pending()
        if policy == "waitlist":
            schedule_offer_responses()
    elapsed = wall.perf_counter() - t0

    # Measure the middle of the run: skip a two-week warm-up and the last two weeks.
    lo = datetime.combine(START.date() + timedelta(days=14), time.min)
    hi = datetime.combine(START.date() + timedelta(days=weeks * 7 - 14), time.min)
    with clinic.Session() as s:
        total = s.query(Slot).filter(Slot.start >= lo, Slot.start < hi).count()
        rows = s.execute(select(Appointment.patient_id, Slot.start).join(Slot)
                         .where(Appointment.status == "booked", Slot.start >= lo, Slot.start < hi)).all()
    waits = [(start - by_pid[pid].at).total_seconds() / 86400 for pid, start in rows]
    # Delay beyond the day the patient asked for, in whole days (the part a clinic can fix).
    delays = [max(0, (start.date() - by_pid[pid].wants_from.date()).days) for pid, start in rows]
    engine.dispose()
    return {"policy": policy, **stats, "slots_measured": total, "visits": len(rows),
            "utilization": len(rows) / total if total else 0, "avg_wait_days": statistics.fmean(waits),
            "median_wait_days": statistics.median(waits), "avg_delay_days": statistics.fmean(delays),
            "seconds": round(elapsed, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--load", type=float, default=1.2, help="demand as a share of capacity")
    ap.add_argument("--cancel-rate", type=float, default=0.15)
    ap.add_argument("--waitlist-rate", type=float, default=0.6, help="share of patients who opt into the waitlist")
    ap.add_argument("--same-day-share", type=float, default=0.15,
                    help="share of requests that want a visit within a day")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="results/simulation.json")
    ap.add_argument("--require-improvement", action="store_true",
                    help="exit with an error unless the waitlist beats the baseline (used in CI)")
    args = ap.parse_args()

    days, requests = make_workload(args.weeks, args.load, args.cancel_rate, args.waitlist_rate, args.seed,
                                   args.same_day_share)
    print(f"{args.weeks} weeks, {len(days)} clinic days, {len(requests):,} appointment requests, "
          f"demand {args.load:.0%} of capacity, {args.cancel_rate:.0%} cancel, "
          f"{args.waitlist_rate:.0%} opt into the waitlist")
    results = [run(p, args.weeks, requests) for p in ("baseline", "waitlist")]
    base, wl = results
    print(f"\n{'':24}{'baseline':>12}{'waitlist':>12}")
    for label, key, fmt in [("events processed", "events", "{:,}"), ("cancellations", "cancellations", "{:,}"),
                            ("waitlist offers", "offers", "{:,}"), ("offers accepted", "accepted", "{:,}"),
                            ("utilization", "utilization", "{:.1%}"), ("avg wait (days)", "avg_wait_days", "{:.2f}"),
                            ("median wait (days)", "median_wait_days", "{:.2f}"),
                            ("delay past wanted date", "avg_delay_days", "{:.2f}"),
                            ("requests not served", "unserved", "{:,}")]:
        print(f"{label:24}{fmt.format(base[key]):>12}{fmt.format(wl[key]):>12}")
    util_gain = (wl["utilization"] - base["utilization"]) / base["utilization"]
    wait_cut = (base["avg_wait_days"] - wl["avg_wait_days"]) / base["avg_wait_days"]
    print(f"\nUtilization {util_gain:+.1%} relative ({(wl['utilization'] - base['utilization']) * 100:+.1f} points); "
          f"average wait {-wait_cut:+.1%}. Total events: {base['events'] + wl['events']:,}.")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"args": vars(args), "results": results,
                                          "utilization_gain_relative": util_gain,
                                          "wait_reduction_relative": wait_cut}, indent=2))
    print(f"Saved to {args.out}")
    if args.require_improvement and not (wl["utilization"] >= base["utilization"]
                                         and wl["avg_delay_days"] <= base["avg_delay_days"]):
        sys.exit("Waitlist auto-fill did not beat the baseline")


if __name__ == "__main__":
    main()
