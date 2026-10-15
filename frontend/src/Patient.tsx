import { FormEvent, useEffect, useState } from "react";
import type { Shell } from "./App";
import {
  addDays, api, CANCEL_LABEL, dayLabel, longDay, MyWaitlist, Overview, parseLocal, Slot, STATUS_LABEL, Suggestion,
  timeLabel, until, Visit, Week,
} from "./api";
import { Legend, WeekGrid, WeekNav } from "./Calendar";
import { Confirm, Countdown, Empty, useData } from "./common";

export const PATIENT_TABS = [
  { id: "home", label: "Home" },
  { id: "book", label: "Book a visit" },
  { id: "history", label: "History" },
];

export function PatientPortal({ shell }: { shell: Shell }) {
  const [overview] = useData<Overview>(() => api.patient.overview(), [shell.version]);
  return (
    <main className="page">
      {shell.tab === "home" && <Home shell={shell} o={overview} />}
      {shell.tab === "book" && <Book shell={shell} o={overview} />}
      {shell.tab === "history" && <History shell={shell} />}
    </main>
  );
}

// ============================================================ shared pieces
function OfferBlock({ shell, offer }: { shell: Shell; offer: Visit }) {
  return (
    <div className="offer-block">
      <p className="kicker-ink">An earlier time opened up</p>
      <h2>{offer.provider} can see you {dayLabel(offer.start)} at {timeLabel(offer.start)}</h2>
      {offer.hold_expires_at && <Countdown until={offer.hold_expires_at} now={shell.now} />}
      <div className="row">
        <button className="primary" onClick={() => shell.run(() => api.patient.accept(offer.id),
          (r) => r.released_appointment_id ? "It's yours. Your later visit went to the next person waiting." : "It's yours.")}>
          Take it
        </button>
        <button className="ghost" onClick={() => shell.run(() => api.patient.decline(offer.id), "Passed. It goes to the next person waiting.")}>
          No thanks
        </button>
      </div>
    </div>
  );
}

function VisitActions({ shell, visit, o }: { shell: Shell; visit: Visit; o: Overview | null }) {
  const [confirming, setConfirming] = useState(false);
  const soon = parseLocal(visit.start).getTime() - shell.now().getTime() < 86400000;
  const asking = o?.waitlist.find((w) => w.replaces_appointment_id === visit.id);
  return (
    <div className="row">
      <button className="ghost" onClick={() => shell.go("book", `move-${visit.id}`)}>Reschedule</button>
      {!asking && (
        <button className="ghost" disabled={soon} title={soon ? "This visit is less than a day away." : undefined}
          onClick={() => shell.run(() => api.patient.joinWaitlist({
            earliest: shell.today, latest: addDays(visit.start.slice(0, 10), -1), provider_id: visit.provider_id,
            time_of_day: "any", replaces_appointment_id: visit.id,
          }), (r) => r.status === "offered" ? "An earlier time was already open. It's held for you." : `You're #${r.position} in line for anything earlier.`)}>
          Get me in sooner
        </button>
      )}
      <button className="danger" onClick={() => setConfirming(true)}>Cancel</button>
      {confirming && (
        <Confirm title="Cancel this visit?" confirm="Cancel visit" danger onCancel={() => setConfirming(false)}
          onConfirm={() => { setConfirming(false); shell.run(() => api.patient.cancel(visit.id), "Cancelled. The time goes to the next person waiting."); }}>
          <p>{visit.provider}, {longDay(visit.start)} at {timeLabel(visit.start)}.</p>
          <p className="muted">The time is offered to someone on the waitlist straight away, so you may not get it back.</p>
        </Confirm>
      )}
    </div>
  );
}

function WaitlistCard({ shell, w }: { shell: Shell; w: MyWaitlist }) {
  return (
    <li className="wl">
      <div>
        <b>{w.position ? `#${w.position} in line` : "In line"} for {w.provider ?? w.specialty}</b>
        <span className="muted small">
          {w.replaces_appointment_id ? "Anything earlier than your current visit" : `${dayLabel(w.earliest)} to ${dayLabel(w.latest)}`}
          {w.time_of_day !== "any" ? `, ${w.time_of_day}s` : ""}
        </span>
        {w.priority > 0 && <span className="tag prio">Priority: your doctor had to cancel</span>}
        {w.status === "offered" && <span className="tag held">Offer waiting for you</span>}
      </div>
      <button className="link" onClick={() => shell.run(() => api.patient.leaveWaitlist(w.id), "You've left the waitlist.")}>Leave</button>
    </li>
  );
}

// ============================================================ home
function Home({ shell, o }: { shell: Shell; o: Overview | null }) {
  if (!o) return <p className="muted pad">Loading…</p>;
  const now = shell.now();
  const rest = o.upcoming.slice(1);
  const first = o.patient.name.split(" ")[0];
  return (
    <div className="home-grid">
      <div className="stack">
        <h1 className="hello">Hi {first}.</h1>
        {o.offers.map((offer) => <OfferBlock key={offer.id} shell={shell} offer={offer} />)}

        {o.next ? (
          <section className="hero card">
            <p className="kicker">Your next visit, {until(o.next.start, now)}</p>
            <h2>{longDay(o.next.start)}<br />{timeLabel(o.next.start)}</h2>
            <p className="hero-who"><b>{o.next.provider}</b> <span className="muted">{o.next.specialty}</span></p>
            <VisitActions shell={shell} visit={o.next} o={o} />
          </section>
        ) : (
          <section className="hero card">
            <p className="kicker">No visits booked</p>
            <h2>When would you like to come in?</h2>
            <button className="primary" onClick={() => shell.go("book")}>Book a visit</button>
          </section>
        )}

        {rest.length > 0 && (
          <section className="card">
            <h3>Also coming up</h3>
            <ul className="visit-list">
              {rest.map((v) => (
                <li key={v.id}>
                  <div><b>{dayLabel(v.start)}, {timeLabel(v.start)}</b><span className="muted">{v.provider}</span></div>
                  <VisitActions shell={shell} visit={v} o={o} />
                </li>
              ))}
            </ul>
          </section>
        )}

        {o.waitlist.length > 0 && (
          <section className="card">
            <h3>Waiting for an earlier time</h3>
            <ul className="wl-list">{o.waitlist.map((w) => <WaitlistCard key={w.id} shell={shell} w={w} />)}</ul>
            <p className="muted small">When a time that fits opens up, it's held for you for 15 minutes and you'll see it at the top of this page.</p>
          </section>
        )}
      </div>

      <div className="stack">
        <section className="card">
          <h3>Last visit</h3>
          {o.last_visit ? (
            <>
              <p><b>{dayLabel(o.last_visit.start)}</b> with {o.last_visit.provider}</p>
              {o.last_visit.note && <blockquote className="note-quote">{o.last_visit.note}</blockquote>}
              <button className="link" onClick={() => shell.go("history")}>See all visits</button>
            </>
          ) : <p className="muted">No visits yet.</p>}
        </section>
        <section className="card">
          <h3>Messages</h3>
          {o.notifications.length === 0 ? <p className="muted">Nothing yet.</p> : (
            <ul className="messages">
              {o.notifications.map((n) => (
                <li key={n.id} className={n.kind}><p>{n.message}</p><span className="muted small">{dayLabel(n.at)}, {timeLabel(n.at)}</span></li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

// ============================================================ book
const LOOK: Record<string, { text: string; kind: string }> = {
  open: { text: "Open", kind: "open" },
  mine: { text: "You", kind: "mine" },
  offer: { text: "Offered to you", kind: "offer" },
  taken: { text: "", kind: "taken" },
  unavailable: { text: "", kind: "off" },
};

function Book({ shell, o }: { shell: Shell; o: Overview | null }) {
  const moveId = shell.param?.startsWith("move-") ? Number(shell.param.slice(5)) : null;
  const moving = moveId ? o?.upcoming.find((v) => v.id === moveId) ?? null : null;
  const fromParam = shell.param && /^\d+$/.test(shell.param) ? Number(shell.param) : null;
  const [providerId, setProviderId] = useState<number | null>(fromParam);
  const [start, setStart] = useState(shell.today);
  const [selected, setSelected] = useState<number | null>(null);
  const pid = providerId ?? moving?.provider_id ?? shell.providers[0]?.id ?? null;
  const provider = shell.providers.find((p) => p.id === pid);
  const [week, error] = useData<Week | null>(() => pid ? api.patient.week(pid, start) : Promise.resolve(null), [pid, start, shell.version]);

  useEffect(() => { setSelected(null); }, [pid, start, moveId]);
  if (!provider) return <p className="muted pad">Loading…</p>;
  const slot = week?.days.flatMap((d) => d.slots).find((s) => s.id === selected) ?? null;

  return (
    <div className="book">
      <section className="card cal-card">
        {moving && (
          <div className="moving">
            <p><b>Moving your visit</b> on {dayLabel(moving.start)} at {timeLabel(moving.start)} with {moving.provider}. Pick a new time.</p>
            <button className="link" onClick={() => shell.go("book")}>Keep it as it is</button>
          </div>
        )}
        <div className="doc-pick" role="radiogroup" aria-label="Doctor">
          {shell.providers.map((p) => (
            <button key={p.id} role="radio" aria-checked={p.id === pid} className={p.id === pid ? "on" : ""} onClick={() => setProviderId(p.id)}>
              <b>{p.name}</b><span>{p.specialty}</span>
            </button>
          ))}
        </div>
        <div className="cal-bar">
          <h2>{provider.name}'s week</h2>
          <WeekNav start={start} today={shell.today} onChange={setStart} />
        </div>
        <Legend items={[["open", "Open"], ["mine", "Yours"], ["offer", "Offered to you"], ["taken", "Taken"], ["off", "Not working"]]} />
        {error ? <div className="cal-empty">Couldn't load the schedule: {error}</div>
          : !week ? <div className="cal-empty muted">Loading the week…</div>
            : <WeekGrid week={week} today={shell.today} now={shell.now()} selected={selected} onSelect={setSelected} trim
              look={(s: Slot) => s.past ? { text: "", kind: "past", label: "past" } : LOOK[s.state] ?? { text: "", kind: "taken" }} />}
      </section>

      <aside className="card side" aria-live="polite">
        {slot ? <SlotPanel shell={shell} slot={slot} provider={provider} moving={moving} o={o} close={() => setSelected(null)} />
          : <FindTime shell={shell} onPick={(p, s, id) => { setProviderId(p); setStart(s.slice(0, 10)); setTimeout(() => setSelected(id), 0); }} />}
      </aside>
    </div>
  );
}

function SlotPanel({ shell, slot, provider, moving, o, close }: {
  shell: Shell; slot: Slot; provider: { id: number; name: string }; moving: Visit | null; o: Overview | null; close: () => void;
}) {
  const mine = slot.state === "mine" ? o?.upcoming.find((v) => v.id === slot.appointment_id) : null;
  const offer = slot.state === "offer" ? o?.offers.find((v) => v.id === slot.appointment_id) : null;
  return (
    <div className="panel">
      <button className="close" aria-label="Close" onClick={close}>×</button>
      <p className="kicker">{provider.name}</p>
      <h2>{dayLabel(slot.start)}<br />{timeLabel(slot.start)}</h2>
      {slot.past ? <p className="muted">This time has passed.</p>
        : slot.state === "open" ? (
          moving ? (
            <button className="primary wide" onClick={() => shell.run(() => api.patient.reschedule(moving.id, slot.id),
              (r) => `Moved to ${r.label}.`).then((r) => { if (r) shell.go("home"); })}>
              Move my visit here
            </button>
          ) : (
            <button className="primary wide" onClick={() => shell.run(() => api.patient.book(slot.id),
              (r) => `Booked: ${r.provider}, ${r.label}.`).then((r) => { if (r) shell.go("home"); })}>
              Book this time
            </button>
          )
        ) : slot.state === "mine" && mine ? (
          <><p><b>This is your visit.</b></p><VisitActions shell={shell} visit={mine} o={o} /></>
        ) : slot.state === "offer" && offer ? <OfferBlock shell={shell} offer={offer} />
          : slot.state === "unavailable" ? <p className="muted">{provider.name} isn't seeing patients at this time.</p>
            : (
              <>
                <p>Someone else has this time.</p>
                <JoinWaitlist shell={shell} providerId={provider.id} name={provider.name} day={slot.start.slice(0, 10)} o={o} />
              </>
            )}
    </div>
  );
}

function JoinWaitlist({ shell, providerId, name, day, o }: { shell: Shell; providerId: number; name: string; day: string; o: Overview | null }) {
  const [tod, setTod] = useState("any");
  const already = o?.waitlist.find((w) => w.provider === name && !w.replaces_appointment_id);
  if (already) return <p className="note">You're already #{already.position} in line for {name}.</p>;
  const latest = day < shell.today ? addDays(shell.today, 7) : day;
  return (
    <div className="join">
      <p className="muted">Join the waitlist and we'll hold the first time that opens with {name} up to {dayLabel(latest)}.</p>
      <label className="inline">Best time
        <select value={tod} onChange={(e) => setTod(e.target.value)}>
          <option value="any">Any time</option><option value="morning">Mornings</option><option value="afternoon">Afternoons</option>
        </select>
      </label>
      <button className="ghost wide" onClick={() => shell.run(() => api.patient.joinWaitlist({
        earliest: shell.today, latest, provider_id: providerId, time_of_day: tod,
      }), (r) => r.status === "offered" ? "A time was already open, so it's held for you now." : `You're #${r.position} in line.`)}>
        Join the waitlist
      </button>
    </div>
  );
}

function FindTime({ shell, onPick }: { shell: Shell; onPick: (providerId: number, start: string, slotId: number) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Suggestion | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    const r = await shell.run(() => api.patient.ask(text));
    if (r) setResult(r);
    setBusy(false);
  }
  const u = result?.understood;
  return (
    <div className="panel">
      <h2 className="h2-small">Find a time</h2>
      <p className="muted">Pick a time on the calendar, or say what you need.</p>
      <form className="ask" onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} maxLength={500}
          placeholder="A skin doctor next Tuesday morning" aria-label="Describe the appointment you need" />
        <button className="primary" disabled={busy}>{busy ? "…" : "Find"}</button>
      </form>
      {result && u && (
        <div className="found">
          <p className="muted small">
            Looking for {[u.provider ?? u.specialty, u.earliest && (u.earliest === u.latest ? dayLabel(u.earliest) : `${dayLabel(u.earliest)} to ${dayLabel(u.latest!)}`),
              u.time_of_day !== "any" ? `${u.time_of_day}s` : null].filter(Boolean).join(", ")}
            {result.parser === "nexusai" ? ` (understood by ${result.gateway?.model ?? "NexusAI"})` : ""}
          </p>
          {result.options.length ? (
            <div className="chips">
              {result.options.map((o) => (
                <button key={o.slot_id} onClick={() => onPick(o.provider_id, o.start, o.slot_id)}>
                  <b>{dayLabel(o.start)}, {timeLabel(o.start)}</b><span>{o.provider}</span>
                </button>
              ))}
            </div>
          ) : <p className="muted">Nothing open fits. Try a wider range, or pick a taken time to join the waitlist.</p>}
        </div>
      )}
      <div className="tips muted small">
        <p><b>Fully booked?</b> Click any taken time to join that doctor's waitlist. If someone cancels, the time is held for you for 15 minutes.</p>
      </div>
    </div>
  );
}

// ============================================================ history
function History({ shell }: { shell: Shell }) {
  const [visits] = useData<Visit[]>(() => api.patient.history(), [shell.version]);
  const [filter, setFilter] = useState("all");
  if (!visits) return <p className="muted pad">Loading…</p>;
  const shown = visits.filter((v) => filter === "all" || v.status === filter);
  const count = (s: string) => visits.filter((v) => v.status === s).length;
  return (
    <section className="card history">
      <div className="cal-bar">
        <h1>Your visits</h1>
        <div className="seg small-seg" role="tablist">
          {[["all", `All ${visits.length}`], ["completed", `Seen ${count("completed")}`], ["no_show", `Missed ${count("no_show")}`],
            ["cancelled", `Cancelled ${count("cancelled")}`]].map(([k, label]) => (
            <button key={k} role="tab" aria-selected={filter === k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{label}</button>
          ))}
        </div>
      </div>
      {shown.length === 0 ? <Empty>No visits here yet.</Empty> : (
        <ol className="timeline">
          {shown.map((v) => (
            <li key={v.id} className={v.status}>
              <div className="when"><b>{parseLocal(v.start).toLocaleDateString([], { month: "short", day: "numeric" })}</b>
                <span className="muted small">{parseLocal(v.start).getFullYear()}</span></div>
              <div className="what">
                <p><b>{v.provider}</b> <span className="muted">{v.specialty}, {timeLabel(v.start)}</span>
                  <span className={`tag st-${v.status}`}>{v.status === "booked" ? "Today" : STATUS_LABEL[v.status]}</span></p>
                {v.note && <blockquote className="note-quote">{v.note}</blockquote>}
                {v.status === "cancelled" && v.cancel_reason && <p className="muted small">{CANCEL_LABEL[v.cancel_reason] ?? "Cancelled"}</p>}
                {v.status === "completed" && !v.note && <p className="muted small">No notes for this visit.</p>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
