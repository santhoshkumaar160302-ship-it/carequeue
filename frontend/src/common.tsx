import { ReactNode, useEffect, useRef, useState } from "react";
import { ActivityEvent, parseLocal, relative } from "./api";

/** Fetch on mount and whenever deps change; keeps the last good data while refetching. */
export function useData<T>(fetcher: () => Promise<T>, deps: unknown[]): [T | null, string | null] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    fetcher()
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return [data, error];
}

export function Countdown({ until, now, minutes = 15 }: { until: string; now: () => Date; minutes?: number }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  const left = Math.max(0, Math.round((parseLocal(until).getTime() - now().getTime()) / 1000));
  return (
    <div className="countdown">
      <div><span>Held for you</span><b>{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</b></div>
      <div className="bar"><i style={{ width: `${Math.min(100, (left / (minutes * 60)) * 100)}%` }} /></div>
    </div>
  );
}

/** A modal that asks before something with consequences (cancelling patients, blocking time). */
export function Confirm({ title, children, confirm, danger, onConfirm, onCancel, busy }: {
  title: string; children: ReactNode; confirm: string; danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal?.();
    return () => d?.close?.();
  }, []);
  return (
    <dialog ref={ref} className="confirm" onCancel={(e) => { e.preventDefault(); onCancel(); }} aria-labelledby="confirm-title">
      <h2 id="confirm-title">{title}</h2>
      <div className="confirm-body">{children}</div>
      <div className="row end">
        <button className="ghost" onClick={onCancel}>Go back</button>
        <button className={danger ? "danger solid" : "primary"} onClick={onConfirm} disabled={busy}>{busy ? "…" : confirm}</button>
      </div>
    </dialog>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

const FEED: Record<string, (e: ActivityEvent) => string> = {
  appointment_booked: (e) => `${e.patient} booked ${e.slot}`,
  slot_freed: (e) => e.cause === "reopened" ? `${e.slot} reopened` : `${e.slot} opened up`,
  hold_offered: (e) => `${e.slot} offered to ${e.patient}`,
  hold_accepted: (e) => `${e.patient} took ${e.slot}`,
  visit_completed: (e) => `Saw ${e.patient}`,
  visit_no_show: (e) => `${e.patient} didn't come`,
};

export function Feed({ events, now, empty }: { events: ActivityEvent[]; now: Date; empty: string }) {
  const shown = events.filter((e) => FEED[e.type]);
  if (!shown.length) return <p className="muted">{empty}</p>;
  return (
    <ul className="feed">
      {shown.map((e) => (
        <li key={e.id} className={e.type}><span className="dot" />
          <span>{FEED[e.type](e)}<span className="muted small"> {relative(e.at, now)}</span></span>
        </li>
      ))}
    </ul>
  );
}
