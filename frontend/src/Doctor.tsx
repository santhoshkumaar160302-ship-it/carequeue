import { FormEvent, useEffect, useState } from "react";
import type { Shell } from "./App";
import {
  LOCALE, ActivityEvent, Affected, api, ChartBrief, NoteCheck, VitalsInput, CANCEL_LABEL, ChangeResult, dayLabel, DoctorPatient, HoursDay, HoursInput, longDay,
  parseLocal, PatientRecord, Preview, Shift, ShiftName, Slot, SlotStory, StatusLabelKey, STATUS_LABEL, StoryEvent, timeLabel, TimeOff,
  TimeOffInput, timeOfDayText, Today, until, Visit, WaitlistRow, Week,
} from "./api";
import { Legend, WeekGrid, WeekNav } from "./Calendar";
import { NoteChecker, NoteFindings, SummaryCard } from "./AiHelp";
import { ChartView } from "./ChartView";
import { Confirm, Empty, Feed, useData } from "./common";

export const DOCTOR_TABS = [
  { id: "today", label: "Today" },
  { id: "calendar", label: "Calendar" },
  { id: "availability", label: "Availability" },
  { id: "patients", label: "Patients" },
];

export function DoctorPortal({ shell }: { shell: Shell }) {
  return (
    <main className="page">
      {shell.tab === "today" && <TodayView shell={shell} />}
      {shell.tab === "calendar" && <CalendarView shell={shell} />}
      {shell.tab === "availability" && <AvailabilityView shell={shell} />}
      {shell.tab === "patients" && <PatientsView shell={shell} />}
    </main>
  );
}

const greeting = (d: Date) => (d.getHours() < 12 ? "Good morning" : d.getHours() < 17 ? "Good afternoon" : "Good evening");
const surname = (name: string) => name.replace("Dr. ", "").split(" ").slice(-1)[0];

/** Mark a visit that has started as seen (with an optional note) or as a no-show. */
const VITAL_FIELDS: { key: keyof VitalsInput; label: string; unit: string; step: string }[] = [
  { key: "systolic", label: "BP systolic", unit: "mmHg", step: "1" },
  { key: "diastolic", label: "BP diastolic", unit: "mmHg", step: "1" },
  { key: "heart_rate", label: "Heart rate", unit: "/min", step: "1" },
  { key: "temperature_c", label: "Temp", unit: "°C", step: "0.1" },
  { key: "weight_kg", label: "Weight", unit: "kg", step: "0.1" },
];

function CloseVisit({ shell, visit, compact }: { shell: Shell; visit: Visit; compact?: boolean }) {
  const [note, setNote] = useState("");
  const [vitals, setVitals] = useState<Record<string, string>>({});
  const [checked, setChecked] = useState<NoteCheck | null>(null);
  const [open, setOpen] = useState(!compact);
  const entered = () => Object.fromEntries(Object.entries(vitals).filter(([, v]) => v !== "").map(([k, v]) => [k, Number(v)]));
  if (!open) {
    return (
      <div className="row">
        <button className="primary small-btn" onClick={() => setOpen(true)}>Mark seen</button>
        <button className="ghost small-btn" onClick={() => shell.run(() => api.doctor.noShow(visit.id), `${visit.patient} marked as not coming.`)}>
          Didn't come
        </button>
      </div>
    );
  }
  return (
    <form className="close-visit" onSubmit={(e) => {
      e.preventDefault();
      const given = entered();
      shell.run(() => api.doctor.complete(visit.id, note, Object.keys(given).length ? given : undefined),
        `Saved ${visit.patient}'s visit.`);
    }}>
      <fieldset className="vitals-in">
        <legend className="muted small">Vital signs (optional)</legend>
        {VITAL_FIELDS.map((f) => (
          <label key={f.key}>
            <span>{f.label}</span>
            <span className="unit-in">
              <input type="number" inputMode="decimal" step={f.step} value={vitals[f.key] ?? ""}
                onChange={(e) => setVitals({ ...vitals, [f.key]: e.target.value })} />
              <span className="muted small">{f.unit}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <label className="sr-only" htmlFor={`note-${visit.id}`}>Visit note</label>
      <textarea id={`note-${visit.id}`} value={note} onChange={(e) => { setNote(e.target.value); setChecked(null); }}
        maxLength={2000} rows={3} placeholder="Visit note (the patient can read it)" />
      {checked && <NoteFindings result={checked} />}
      <div className="row">
        <button className="primary small-btn">Save as seen</button>
        <NoteChecker shell={shell} appointmentId={visit.id} note={note} vitals={entered()} onResult={setChecked} />
        <button type="button" className="ghost small-btn" onClick={() => shell.run(() => api.doctor.noShow(visit.id), `${visit.patient} marked as not coming.`)}>
          Didn't come
        </button>
        {compact && <button type="button" className="link" onClick={() => setOpen(false)}>Not now</button>}
      </div>
    </form>
  );
}

/** "12 HR Hydrocodone Bitartrate 10 MG Extended Release Oral Capsule" -> "Hydrocodone Bitartrate". */
function drugName(full: string): string {
  const words = full.replace(/[{}()[\],]/g, " ").split(/\s+/).filter(Boolean);
  const start = words.findIndex((w) => /^[a-z][a-z-]{3,}$/i.test(w) && !/^(actuat|nda\d*|ml|mg|hr|unt|pack)$/i.test(w));
  if (start < 0) return full.slice(0, 24);
  const name = [];
  for (const w of words.slice(start)) {
    if (/\d/.test(w) || /^(mg|ml|oral|tablet|capsule|solution|injectable|inhaler|extended|release)$/i.test(w)) break;
    name.push(w);
  }
  return name.slice(0, 2).join(" ");
}

function ChartBriefView({ brief }: { brief: ChartBrief }) {
  return (
    <div className="brief">
      <p>
        <span className="brief-label">Allergies</span>
        {brief.allergies.length ? brief.allergies.map((a) => <span key={a} className="tag risk">{a}</span>)
          : <span className="muted">None recorded</span>}
      </p>
      {brief.conditions.length > 0 && (
        <p><span className="brief-label">Conditions</span>{brief.conditions.slice(0, 4).join(", ")}
          {brief.conditions.length > 4 && <span className="muted small">and {brief.conditions.length - 4} more</span>}</p>
      )}
      {brief.medications.length > 0 && (
        <p><span className="brief-label">Medications</span>{brief.medications.length} active
          <span className="muted small"> ({brief.medications.slice(0, 2).map(drugName).join(", ")}
            {brief.medications.length > 2 ? ", …" : ""})</span></p>
      )}
    </div>
  );
}

// ============================================================ today
function TodayView({ shell }: { shell: Shell }) {
  const [today] = useData<Today>(() => api.doctor.today(), [shell.version]);
  const [line] = useData<WaitlistRow[]>(() => api.doctor.waitlist(), [shell.version]);
  const [activity] = useData<ActivityEvent[]>(() => api.doctor.activity(), [shell.version]);
  if (!today) return <p className="muted pad">Loading…</p>;
  const now = shell.now();
  const n = today.next;
  const c = today.counts;
  return (
    <div className="home-grid">
      <div className="stack">
        <div>
          <h1 className="hello">{greeting(now)}, Dr. {surname(today.provider.name)}.</h1>
          <p className="muted">{longDay(today.date)}</p>
        </div>
        <div className="stats">
          <div><b>{c.booked}</b><span>visits today</span></div>
          <div><b>{c.done}</b><span>seen</span></div>
          <div className={c.to_close ? "warn" : ""}><b>{c.to_close}</b><span>to write up</span></div>
          <div><b>{c.open_today}</b><span>open times left</span></div>
          <div><b>{c.waitlist}</b><span>on your waitlist</span></div>
        </div>

        {n ? (
          <section className="hero card">
            <p className="kicker">{n.now ? "With you now" : `Next patient, ${until(n.start, now)}`}</p>
            <h2>{n.patient}</h2>
            <p className="hero-who"><b>{dayLabel(n.start)}, {timeLabel(n.start)}</b> <span className="muted">{n.phone}</span></p>
            {n.chart && <ChartBriefView brief={n.chart} />}
            <SummaryCard key={n.patient_id} shell={shell} patientId={n.patient_id!} compact />
            {n.last_visit ? (
              <div className="last">
                <p className="muted small">Last visit with you, {dayLabel(n.last_visit.start)}</p>
                {n.last_visit.note ? <blockquote className="note-quote">{n.last_visit.note}</blockquote> : <p className="muted">No note.</p>}
              </div>
            ) : <p className="muted">First visit with you.</p>}
            <div className="row">
              <button className="ghost" onClick={() => shell.go("patients", n.patient_id)}>Open record</button>
              {n.now && <CloseVisit shell={shell} visit={n} compact />}
            </div>
          </section>
        ) : (
          <section className="hero card"><p className="kicker">Next patient</p><h2>Nobody else is booked.</h2></section>
        )}

        {today.earlier.length > 0 && (
          <section className="card">
            <h3>Still to write up from earlier</h3>
            <ul className="day-list">
              {today.earlier.map((v) => (
                <li key={v.id} className="booked">
                  <span className="t">{dayLabel(v.start)}<br />{timeLabel(v.start)}</span>
                  <div className="who-col">
                    <button className="link name" onClick={() => shell.go("patients", v.patient_id!)}>{v.patient}</button>
                    <CloseVisit shell={shell} visit={v} compact />
                  </div>
                  <span className="tag st-booked">To write up</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="card">
          <h3>Today's visits</h3>
          {today.visits.length === 0 ? <p className="muted">No visits today.</p> : (
            <ul className="day-list">
              {today.visits.map((v) => (
                <li key={v.id} className={`${v.status} ${v.now ? "is-now" : ""}`}>
                  <span className="t">{timeLabel(v.start)}</span>
                  <div className="who-col">
                    <button className="link name" onClick={() => shell.go("patients", v.patient_id!)}>{v.patient}</button>
                    {v.note && <span className="muted small clamp">{v.note}</span>}
                    {v.status === "booked" && v.past && <CloseVisit shell={shell} visit={v} compact />}
                  </div>
                  <span className={`tag st-${v.status}`}>{v.status === "booked" ? (v.now ? "Now" : v.past ? "To write up" : "Booked")
                    : STATUS_LABEL[v.status as StatusLabelKey]}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <div className="stack">
        <section className="card">
          <h3>Your waitlist</h3>
          {!line?.length ? <p className="muted">Nobody is waiting.</p> : (
            <ol className="line">
              {line.map((w, i) => (
                <li key={w.id}>
                  <span className="n">{i + 1}</span>
                  <span><b>{w.patient}</b><span className="muted small">
                    {dayLabel(w.earliest)} to {dayLabel(w.latest)}{w.time_of_day !== "any" ? `, ${timeOfDayText(w.time_of_day)}` : ""}
                  </span></span>
                  {w.status === "offered" ? <span className="tag held">offer out</span> : w.priority > 0 ? <span className="tag prio">rebooking</span> : <span />}
                </li>
              ))}
            </ol>
          )}
          <p className="muted small">When a time frees up, the first person who fits gets 15 minutes to take it.</p>
        </section>
        <section className="card">
          <h3>Recent activity</h3>
          <Feed events={(activity ?? []).slice(0, 8)} now={now} empty="Quiet so far." />
        </section>
      </div>
    </div>
  );
}

// ============================================================ calendar
function look(s: Slot) {
  const name = s.patient ?? "";
  switch (s.state) {
    case "open": return s.past ? { text: "", kind: "past", label: "past, empty" } : { text: "Open", kind: "open" };
    case "booked": return { text: name, kind: s.past ? "taken past-booked" : "taken doc", label: `${name}, booked` };
    case "held": return { text: `Offered: ${name}`, kind: "held" };
    case "completed": return { text: name, kind: "done", label: `${name}, seen` };
    case "no_show": return { text: name, kind: "missed", label: `${name}, didn't come` };
    case "blocked": return s.blocked_reason === "time_off" ? { text: "Time off", kind: "off timeoff" } : { text: "", kind: "off", label: "outside your hours" };
    default: return { text: name, kind: "taken" };
  }
}

function CalendarView({ shell }: { shell: Shell }) {
  const [start, setStart] = useState(shell.today);
  const [selected, setSelected] = useState<number | null>(null);
  const [week, error] = useData<Week>(() => api.doctor.week(start), [start, shell.version]);
  useEffect(() => setSelected(null), [start]);
  return (
    <div className="book">
      <section className="card cal-card">
        <div className="cal-bar">
          <h1>Your week</h1>
          <WeekNav start={start} today={shell.today} onChange={setStart} />
        </div>
        <Legend items={[["open", "Open"], ["doc", "Booked"], ["held", "Offered to the waitlist"], ["done", "Seen"],
          ["missed", "Didn't come"], ["timeoff", "Time off"], ["off", "Outside your hours"]]} />
        {error ? <div className="cal-empty">Couldn't load your week: {error}</div>
          : !week ? <div className="cal-empty muted">Loading…</div>
            : <WeekGrid week={week} today={shell.today} now={shell.now()} selected={selected} onSelect={setSelected} look={look} trim />}
      </section>
      <aside className="card side" aria-live="polite">
        {selected ? <DoctorSlot shell={shell} slotId={selected} close={() => setSelected(null)} />
          : (
            <div className="panel">
              <h2 className="h2-small">Pick any time</h2>
              <p className="muted">See who's booked, what happened to that time, and who's next in line for it.</p>
              <p className="muted">To change when you work, or to block time, go to <button className="link" onClick={() => shell.go("availability")}>Availability</button>.</p>
            </div>
          )}
      </aside>
    </div>
  );
}

const STORY: Record<string, (e: StoryEvent) => string> = {
  appointment_booked: (e) => `${e.patient} booked it`,
  slot_freed: (e) => e.cause === "cancelled" ? "Cancelled, so it opened up"
    : e.cause === "moved_up" ? "Opened up when its patient moved to an earlier time"
      : e.cause === "declined" ? "The offer was declined" : e.cause === "expired" ? "Nobody replied in time"
        : e.cause === "reopened" ? "You opened this time" : "Opened up",
  hold_offered: (e) => `Offered to ${e.patient}, first in line`,
  hold_accepted: (e) => `${e.patient} took it`,
  visit_completed: (e) => `You saw ${e.patient}`,
  visit_no_show: (e) => `${e.patient} didn't come`,
};
const WORKER: Record<string, string> = {
  "nobody on the waitlist matches": "Nobody waiting fits, so it stayed open.",
  "too soon to offer": "Too close to start time to offer.",
  "already filled": "Someone had already booked it.",
  "doctor unavailable": "You weren't available then.",
};

function DoctorSlot({ shell, slotId, close }: { shell: Shell; slotId: number; close: () => void }) {
  const [story] = useData<SlotStory>(() => api.doctor.slot(slotId), [slotId, shell.version]);
  const [week] = useData<Week>(() => api.doctor.week(story?.slot.start.slice(0, 10) ?? shell.today), [story?.slot.start, shell.version]);
  const [confirm, setConfirm] = useState(false);
  if (!story || story.slot.id !== slotId) return <div className="panel"><p className="muted">Loading…</p></div>;
  const { slot, appointment: a } = story;
  const cell = week?.days.flatMap((d) => d.slots).find((s) => s.id === slotId);
  const visit: Visit | null = cell?.appointment_id ? {
    id: cell.appointment_id, status: cell.state as Visit["status"], source: "", slot_id: slotId, start: slot.start, end: slot.end,
    label: slot.label, note: null, cancel_reason: null, hold_expires_at: null, patient: cell.patient ?? undefined,
    patient_id: cell.patient_id ?? undefined,
  } : null;

  return (
    <div className="panel">
      <button className="close" aria-label="Close" onClick={close}>×</button>
      <p className="kicker">{longDay(slot.start)}</p>
      <h2>{timeLabel(slot.start)}{cell?.patient ? <><br />{cell.patient}</> : null}</h2>
      {cell?.state === "blocked" && <p className="muted">{cell.blocked_reason === "time_off" ? "You blocked this time." : "Outside your working hours."}</p>}
      {cell?.state === "completed" && <p>You saw {cell.patient}.</p>}
      {cell?.state === "no_show" && <p>{cell.patient} didn't come.</p>}
      {a?.status === "held" && <p>Offered to <b>{a.patient}</b> from the waitlist{a.hold_expires_at ? ` until ${timeLabel(a.hold_expires_at)}` : ""}.</p>}
      {cell?.state === "open" && !slot.past && <p>Open. {story.next_in_line.length ? `If someone cancels nearby, ${story.next_in_line[0].patient} is first in line.` : ""}</p>}
      {visit && cell?.patient_id && (
        <div className="row">
          <button className="ghost" onClick={() => shell.go("patients", cell.patient_id!)}>Open record</button>
          {cell.state === "booked" && !slot.past && <button className="danger" onClick={() => setConfirm(true)}>I can't see them</button>}
        </div>
      )}
      {cell?.state === "booked" && visit && slot.past && <div className="sp"><CloseVisit shell={shell} visit={visit} /></div>}
      {confirm && visit && (
        <Confirm title={`Cancel ${visit.patient}'s visit?`} confirm="Cancel and rebook" danger onCancel={() => setConfirm(false)}
          onConfirm={() => { setConfirm(false); shell.run(() => api.doctor.cancel(visit.id),
            (r) => r.offered_appointment_id ? `${visit.patient} was told and offered a new time.` : `${visit.patient} was told and is first in line for the next opening.`); }}>
          <p>{visit.patient} will get a message, go to the front of your waitlist, and be offered the next free time that fits.</p>
          <p className="muted">The freed time is offered to the waitlist as usual.</p>
        </Confirm>
      )}

      <h3>What happened to this time</h3>
      {story.events.length === 0 ? <p className="muted">{a ? "Booked when the schedule opened. Nothing since." : "Nothing yet."}</p> : (
        <ol className="story">
          {story.events.map((e, i) => (
            <li key={i} className={e.type}>
              <span className="dot" />
              <div>
                <p>{(STORY[e.type] ?? ((x: StoryEvent) => x.type))(e)}</p>
                <span className="muted small">{dayLabel(e.at)}, {timeLabel(e.at)}{e.type === "slot_freed" && e.result && WORKER[e.result] ? `. ${WORKER[e.result]}` : ""}</span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ============================================================ availability
// Every half hour of the day, 00:00 to 24:00 (midnight at the end of the day).
const TIMES = Array.from({ length: 49 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`);
const hhmmLabel = (t: string) => (t === "24:00" ? "12:00 AM" : timeLabel(`2000-01-01T${t}`));

function AffectedList({ preview, verb }: { preview: Preview; verb: string }) {
  return (
    <>
      <p>This {verb} <b>{preview.blocked} bookable time{preview.blocked === 1 ? "" : "s"}</b>
        {preview.opened ? <> and opens <b>{preview.opened}</b></> : null}.</p>
      {preview.affected.length > 0 ? (
        <>
          <p><b>{preview.affected.length} patient{preview.affected.length === 1 ? " is" : "s are"} booked in that time:</b></p>
          <ul className="affected">{preview.affected.map((a: Affected) => <li key={a.appointment_id}>{a.patient}, {a.label}</li>)}</ul>
          <p className="muted">Each one gets a message, goes to the front of your waitlist, and is offered the next free time that fits.</p>
        </>
      ) : <p className="muted">Nobody is booked in that time.</p>}
    </>
  );
}

function resultMessage(r: ChangeResult) {
  const moved = r.rebooking.length;
  return moved ? `Saved. ${moved} patient${moved === 1 ? " was" : "s were"} told and ${moved === 1 ? "is" : "are"} being offered new times.` : "Saved.";
}

function AvailabilityView({ shell }: { shell: Shell }) {
  return (
    <div className="avail">
      <WeeklyHours shell={shell} />
      <TimeOffCard shell={shell} />
    </div>
  );
}

function toInput(d: HoursDay): HoursInput {
  return { weekday: d.weekday, working: d.working, shift: d.shift ?? "day", start: d.start ?? "09:00", end: d.end ?? "17:00" };
}

/** "6 AM – 2 PM", with "(next day)" when the shift runs past midnight. */
function rangeLabel(start: string, end: string) {
  const overnight = end !== "24:00" && end <= start;
  return `${hhmmLabel(start)} – ${hhmmLabel(end)}${overnight ? " (next day)" : ""}`;
}

function WeeklyHours({ shell }: { shell: Shell }) {
  const [saved] = useData<HoursDay[]>(() => api.doctor.hours(), []);
  const [shifts] = useData<Shift[]>(() => api.doctor.shifts(), []);
  const [days, setDays] = useState<HoursInput[] | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (saved) setDays(saved.map(toInput));
  }, [saved]);
  if (!saved || !days || !shifts) return <section className="card"><p className="muted">Loading…</p></section>;
  const dirty = JSON.stringify(days) !== JSON.stringify(saved.map(toInput));
  const set = (i: number, patch: Partial<HoursInput>) => setDays(days.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const pickShift = (i: number, name: ShiftName) => {
    const preset = shifts.find((s) => s.name === name);
    set(i, preset ? { shift: name, start: preset.start, end: preset.end } : { shift: "custom" });
  };

  async function review() {
    setBusy(true);
    const p = await shell.run(() => api.doctor.previewHours(days!));
    setBusy(false);
    if (p) setPreview(p);
  }
  async function save() {
    setBusy(true);
    const r = await shell.run(() => api.doctor.setHours(days!), resultMessage);
    setBusy(false);
    setPreview(null);
    if (r) setDays(r.hours.map(toInput));
  }

  return (
    <section className="card">
      <h2 className="h2-small">Weekly hours</h2>
      <p className="muted">Pick a shift for each day you work, or set your own hours. The clinic runs around the clock
        in 30-minute visits; patients can book you only inside your hours.</p>
      <div className="shift-key">
        {shifts.map((s) => <span key={s.name}><b>{s.label}</b> {rangeLabel(s.start, s.end)}</span>)}
      </div>
      <div className="hours">
        {days.map((d, i) => (
          <div key={d.weekday} className={`hours-row ${d.working ? "" : "off"}`}>
            <label className="switch">
              <input type="checkbox" checked={d.working} onChange={(e) => set(i, { working: e.target.checked })} />
              <span>{saved[i].name}</span>
            </label>
            {d.working ? (
              <span className="range">
                <select aria-label={`${saved[i].name} shift`} value={d.shift} onChange={(e) => pickShift(i, e.target.value as ShiftName)}>
                  {shifts.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}
                  <option value="custom">Custom hours</option>
                </select>
                {d.shift === "custom" ? (
                  <>
                    <select aria-label={`${saved[i].name} start`} value={d.start ?? "09:00"} onChange={(e) => set(i, { start: e.target.value })}>
                      {TIMES.slice(0, -1).map((t) => <option key={t} value={t}>{hhmmLabel(t)}</option>)}
                    </select>
                    <span className="muted">to</span>
                    <select aria-label={`${saved[i].name} end`} value={d.end ?? "17:00"} onChange={(e) => set(i, { end: e.target.value })}>
                      {TIMES.slice(1).map((t) => <option key={t} value={t}>{t === "24:00" ? "Midnight" : hhmmLabel(t)}</option>)}
                    </select>
                    {d.end !== "24:00" && (d.end ?? "") <= (d.start ?? "") && <span className="muted small">ends next day</span>}
                  </>
                ) : <span className="muted">{rangeLabel(d.start ?? "", d.end ?? "")}</span>}
              </span>
            ) : <span className="muted">Not working</span>}
          </div>
        ))}
      </div>
      <div className="row">
        <button className="primary" disabled={!dirty || busy} onClick={review}>Save hours</button>
        {dirty && <button className="link" onClick={() => setDays(saved.map(toInput))}>Undo changes</button>}
      </div>
      {preview && (
        <Confirm title="Save your new hours?" confirm={preview.affected.length ? "Save and rebook them" : "Save"} danger={preview.affected.length > 0}
          busy={busy} onCancel={() => setPreview(null)} onConfirm={save}>
          <AffectedList preview={preview} verb="change closes" />
        </Confirm>
      )}
    </section>
  );
}

function TimeOffCard({ shell }: { shell: Shell }) {
  const [list] = useData<TimeOff[]>(() => api.doctor.timeOff(), [shell.version]);
  const [form, setForm] = useState({ from: shell.today, to: shell.today, allDay: true, start: "09:00", end: "12:00", reason: "" });
  const [pending, setPending] = useState<{ body: TimeOffInput; preview: Preview } | null>(null);
  const [busy, setBusy] = useState(false);

  function body(): TimeOffInput {
    if (form.allDay) {
      const end = parseLocal(form.to < form.from ? form.from : form.to);
      end.setDate(end.getDate() + 1);
      const endIso = `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, "0")}-${String(end.getDate()).padStart(2, "0")}`;
      return { start: `${form.from}T00:00`, end: `${endIso}T00:00`, reason: form.reason };
    }
    return { start: `${form.from}T${form.start}`, end: `${form.from}T${form.end}`, reason: form.reason };
  }
  async function review(e: FormEvent) {
    e.preventDefault();
    const b = body();
    setBusy(true);
    const preview = await shell.run(() => api.doctor.previewTimeOff(b));
    setBusy(false);
    if (preview) setPending({ body: b, preview });
  }
  async function save() {
    if (!pending) return;
    setBusy(true);
    const r = await shell.run(() => api.doctor.addTimeOff(pending.body), resultMessage);
    setBusy(false);
    setPending(null);
    if (r) setForm({ ...form, reason: "" });
  }

  return (
    <section className="card">
      <h2 className="h2-small">Time off</h2>
      <p className="muted">Block a morning, a day or a week. Patients already booked are rebooked first.</p>
      <form className="toff-form" onSubmit={review}>
        <label>From<input type="date" required min={shell.today} value={form.from}
          onChange={(e) => setForm({ ...form, from: e.target.value, to: form.to < e.target.value ? e.target.value : form.to })} /></label>
        {form.allDay
          ? <label>To<input type="date" required min={form.from} value={form.to} onChange={(e) => setForm({ ...form, to: e.target.value })} /></label>
          : (
            <span className="range">
              <label>Start<select value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })}>
                {TIMES.slice(0, -1).map((t) => <option key={t} value={t}>{hhmmLabel(t)}</option>)}</select></label>
              <label>End<select value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })}>
                {TIMES.slice(1).map((t) => <option key={t} value={t}>{hhmmLabel(t)}</option>)}</select></label>
            </span>
          )}
        <label className="switch"><input type="checkbox" checked={form.allDay} onChange={(e) => setForm({ ...form, allDay: e.target.checked })} /><span>All day</span></label>
        <label className="grow"><span>Reason <span className="muted">(only you see it)</span></span>
          <input value={form.reason} maxLength={100} placeholder="Conference, training, leave…" onChange={(e) => setForm({ ...form, reason: e.target.value })} /></label>
        <button className="primary" disabled={busy}>Block this time</button>
      </form>

      <h3>Coming up</h3>
      {!list ? <p className="muted">Loading…</p> : list.length === 0 ? <p className="muted">No time off planned.</p> : (
        <ul className="toff-list">
          {list.map((t) => (
            <li key={t.id}>
              <div><b>{t.label}</b>{t.reason && <span className="muted">{t.reason}</span>}</div>
              <button className="link" onClick={() => shell.run(() => api.doctor.removeTimeOff(t.id),
                (r) => `Removed. ${r.opened} time${r.opened === 1 ? "" : "s"} reopened and offered to your waitlist.`)}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {pending && (
        <Confirm title="Block this time?" confirm={pending.preview.affected.length ? "Block and rebook them" : "Block it"}
          danger={pending.preview.affected.length > 0} busy={busy} onCancel={() => setPending(null)} onConfirm={save}>
          <AffectedList preview={pending.preview} verb="blocks" />
        </Confirm>
      )}
    </section>
  );
}

// ============================================================ patients
type Filter = "all" | "mine" | "new";
const FILTERS: { id: Filter; label: string; hint: string }[] = [
  { id: "all", label: "All", hint: "Everyone you've seen, are booked with, or have waiting" },
  { id: "mine", label: "My patients", hint: "Patients you've already seen at least once" },
  { id: "new", label: "New", hint: "Booked with you or on your waitlist, but not seen by you yet" },
];

function patientLine(p: DoctorPatient): string {
  if (p.group === "mine") {
    const seen = `${p.visits} visit${p.visits === 1 ? "" : "s"}, last ${p.last_visit ? dayLabel(p.last_visit.start) : ""}`;
    return p.next_visit ? `Next ${dayLabel(p.next_visit.start)} · ${seen}` : seen;
  }
  if (p.next_visit) return `${p.next_visit.offer ? "Offered" : "First visit"} ${dayLabel(p.next_visit.start)}`;
  if (p.waiting) return "On your waitlist";
  return "Not seen yet";
}

function PatientsView({ shell }: { shell: Shell }) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>(() => {
    try { return (localStorage.getItem("cq.patientFilter") as Filter) || "all"; } catch { return "all"; }
  });
  useEffect(() => { const t = setTimeout(() => setQuery(q), 250); return () => clearTimeout(t); }, [q]);
  useEffect(() => { try { localStorage.setItem("cq.patientFilter", filter); } catch { /* storage unavailable */ } }, [filter]);
  // One request for everyone; the filter and its counts are worked out here, so switching is instant.
  const [people] = useData<DoctorPatient[]>(() => api.doctor.patients(query), [query, shell.version]);
  const selected = shell.param ? Number(shell.param) : null;
  const count = (f: Filter) => (people ?? []).filter((p) => f === "all" || p.group === f).length;
  const shown = (people ?? []).filter((p) => filter === "all" || p.group === filter);
  return (
    <div className={`patients ${selected ? "has-sel" : ""}`}>
      <section className="card plist">
        <h1>Your patients</h1>
        <div className="seg small-seg" role="tablist" aria-label="Which patients">
          {FILTERS.map((f) => (
            <button key={f.id} role="tab" aria-selected={filter === f.id} className={filter === f.id ? "on" : ""}
              title={f.hint} onClick={() => setFilter(f.id)}>
              {f.label} <span className="count">{people ? count(f.id) : ""}</span>
            </button>
          ))}
        </div>
        <p className="muted small filter-hint">{FILTERS.find((f) => f.id === filter)!.hint}.</p>
        <input className="search" type="search" placeholder="Search by name" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search patients" />
        {!people ? <p className="muted">Loading…</p> : shown.length === 0 ? (
          <p className="muted">{q ? "Nobody found." : filter === "new" ? "No new patients right now." : "Nobody here yet."}</p>
        ) : (
          <ul>
            {shown.slice(0, 200).map((p) => (
              <li key={p.id}>
                <a href={`#/patients/${p.id}`} className={p.id === selected ? "on" : ""} aria-current={p.id === selected ? "true" : undefined}>
                  <span className="pname"><b>{p.name}</b>{p.group === "new" && <span className="tag new">New</span>}</span>
                  <span className="muted small">{patientLine(p)}</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="card prec">
        {selected ? <PatientRecordView shell={shell} id={selected} /> : <Empty>Pick a patient to see their visits and notes with you.</Empty>}
      </section>
    </div>
  );
}

function PatientRecordView({ shell, id }: { shell: Shell; id: number }) {
  const [rec, error] = useData<PatientRecord>(() => api.doctor.patient(id), [id, shell.version]);
  if (error) return <Empty>{error}</Empty>;
  if (!rec || rec.id !== id) return <p className="muted">Loading…</p>;
  return <RecordTabs shell={shell} rec={rec} />;
}

function RecordTabs({ shell, rec }: { shell: Shell; rec: PatientRecord }) {
  const [view, setView] = useState<"chart" | "visits">("chart");
  return (
    <div>
      <a className="link back" href="#/patients">‹ All patients</a>
      <h2>{rec.name} {rec.group === "new" && <span className="tag new">New patient</span>}</h2>
      <p className="muted">{rec.phone} · {rec.group === "new" ? "not seen by you yet" : `${rec.visits} visit${rec.visits === 1 ? "" : "s"} with you`}</p>
      <div className="seg small-seg rec-tabs" role="tablist">
        <button role="tab" aria-selected={view === "chart"} className={view === "chart" ? "on" : ""} onClick={() => setView("chart")}>Chart</button>
        <button role="tab" aria-selected={view === "visits"} className={view === "visits" ? "on" : ""} onClick={() => setView("visits")}>
          Visits with you
        </button>
      </div>
      {view === "chart" ? (
        <>
          <SummaryCard key={rec.id} shell={shell} patientId={rec.id} />
          <ChartView shell={shell} patientId={rec.id} editable />
        </>
      ) : <RecordVisits shell={shell} rec={rec} />}
    </div>
  );
}

function RecordVisits({ shell, rec }: { shell: Shell; rec: PatientRecord }) {
  return (
    <div>
      {rec.waiting.length > 0 && (
        <div className="note-box">
          {rec.waiting.map((w) => (
            <p key={w.id}>
              <b>{w.status === "offered" ? "A time is being offered to them" : "On your waitlist"}</b>{" "}
              for {dayLabel(w.earliest)} to {dayLabel(w.latest)}{w.time_of_day !== "any" ? `, ${timeOfDayText(w.time_of_day)}` : ""}
              {w.priority > 0 ? ". Rebooking first, because a visit with you was cancelled." : "."}
            </p>
          ))}
        </div>
      )}
      <h3>Coming up</h3>
      {rec.upcoming.length === 0 ? <p className="muted">Nothing booked.</p> : (
        <ul className="visit-list">{rec.upcoming.map((v) => (
          <li key={v.id}><div><b>{dayLabel(v.start)}, {timeLabel(v.start)}</b>
            <span className="muted">{v.status === "held" ? "Offered from the waitlist" : v.source === "waitlist" ? "Moved up from the waitlist" : "Booked"}</span></div></li>
        ))}</ul>
      )}
      <h3>History with you</h3>
      {rec.history.length === 0 ? <p className="muted">{rec.group === "new" ? "You haven't seen them before." : "No past visits."}</p> : (
        <ol className="timeline">
          {rec.history.map((v) => <RecordVisit key={v.id} shell={shell} v={v} name={rec.name} />)}
        </ol>
      )}
      <p className="muted small">Only your own visits and notes are shown. Other doctors' notes stay with them.</p>
    </div>
  );
}

function RecordVisit({ shell, v, name }: { shell: Shell; v: Visit; name: string }) {
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState(v.note ?? "");
  const [checked, setChecked] = useState<NoteCheck | null>(null);
  return (
    <li className={v.status}>
      <div className="when"><b>{parseLocal(v.start).toLocaleDateString(LOCALE, { month: "short", day: "numeric" })}</b>
        <span className="muted small">{parseLocal(v.start).getFullYear()}</span></div>
      <div className="what">
        <p><span className="muted">{timeLabel(v.start)}</span>
          <span className={`tag st-${v.status}`}>{v.status === "booked" ? "To write up" : STATUS_LABEL[v.status as StatusLabelKey]}</span></p>
        {v.status === "booked" && <CloseVisit shell={shell} visit={{ ...v, patient: name }} compact />}
        {v.status === "cancelled" && v.cancel_reason && <p className="muted small">{CANCEL_LABEL[v.cancel_reason]?.replace("You cancelled", "The patient cancelled") ?? "Cancelled"}</p>}
        {v.status === "completed" && (editing ? (
          <form className="close-visit" onSubmit={(e) => { e.preventDefault(); shell.run(() => api.doctor.note(v.id, note), "Note saved.").then(() => setEditing(false)); }}>
            <textarea value={note} onChange={(e) => { setNote(e.target.value); setChecked(null); }} rows={3} maxLength={2000}
              aria-label="Visit note" />
            {checked && <NoteFindings result={checked} />}
            <div className="row">
              <button className="primary small-btn">Save note</button>
              <NoteChecker shell={shell} appointmentId={v.id} note={note} onResult={setChecked} />
              <button type="button" className="link" onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </form>
        ) : (
          <>
            {v.note ? <blockquote className="note-quote">{v.note}</blockquote> : <p className="muted small">No note.</p>}
            <button className="link small" onClick={() => setEditing(true)}>{v.note ? "Edit note" : "Add a note"}</button>
          </>
        ))}
      </div>
    </li>
  );
}
