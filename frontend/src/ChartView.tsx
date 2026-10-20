import { FormEvent, ReactNode, useState } from "react";
import type { Shell } from "./App";
import {
  AccessEntry, api, ChartAllergy, ChartData, ChartMedication, dateLabel, relative, VitalsRow,
} from "./api";
import { useData } from "./common";

/** The patient chart. Doctors (editable) see it on a patient's record; patients see their own, read-only. */
export function ChartView({ shell, patientId, editable }: { shell: Shell; patientId?: number; editable?: boolean }) {
  const [chart, error] = useData<ChartData>(
    () => (editable && patientId ? api.doctor.chart(patientId) : api.patient.chart()), [patientId, shell.version]);
  if (error) return <p className="muted">{error}</p>;
  if (!chart) return <p className="muted">Loading the chart…</p>;
  const pid = chart.patient.id;
  const activeAllergies = chart.allergies.filter((a) => a.status === "active");
  const pastAllergies = chart.allergies.filter((a) => a.status !== "active");
  const activeConditions = chart.conditions.filter((c) => c.status === "active");
  const pastConditions = chart.conditions.filter((c) => c.status !== "active");
  const activeMeds = chart.medications.filter((m) => m.status === "active");
  const pastMeds = chart.medications.filter((m) => m.status !== "active");
  const you = !editable;

  return (
    <div className="chart">
      <Section title="Allergies" count={activeAllergies.length}
        add={editable ? (done) => <AddAllergy shell={shell} patientId={pid} done={done} /> : undefined}>
        {activeAllergies.length === 0 ? <p className="muted">{you ? "No allergies on file." : "No known allergies recorded."}</p> : (
          <ul className="allergies">
            {activeAllergies.map((a) => (
              <li key={a.id} className={a.criticality === "high" ? "high" : ""}>
                <AllergyText a={a} />
                {editable && <button className="link small" onClick={() => shell.run(() => api.doctor.allergyStatus(a.id, "inactive"),
                  `${a.substance} marked as no longer active.`)}>Mark inactive</button>}
              </li>
            ))}
          </ul>
        )}
        {pastAllergies.length > 0 && (
          <Past label={`${pastAllergies.length} inactive`}>
            {pastAllergies.map((a) => (
              <li key={a.id}><AllergyText a={a} />
                {editable && <button className="link small" onClick={() => shell.run(() => api.doctor.allergyStatus(a.id, "active"),
                  `${a.substance} is active again.`)}>Make active</button>}
              </li>
            ))}
          </Past>
        )}
      </Section>

      <Section title="Conditions" count={activeConditions.length}
        add={editable ? (done) => <AddOne shell={shell} label="Condition" placeholder="e.g. Essential hypertension"
          save={(name) => api.doctor.addCondition(pid, name)} done={done} /> : undefined}>
        {activeConditions.length === 0 ? <p className="muted">No ongoing conditions.</p> : (
          <ul className="items">
            {activeConditions.map((c) => (
              <li key={c.id}>
                <span><b>{c.name}</b><span className="muted small">{c.onset ? `since ${dateLabel(c.onset)}` : ""}{by(c.recorded_by)}</span></span>
                {editable && <button className="link small" onClick={() => shell.run(() => api.doctor.resolveCondition(c.id),
                  `${c.name} marked as resolved.`)}>Resolve</button>}
              </li>
            ))}
          </ul>
        )}
        {pastConditions.length > 0 && (
          <Past label={`${pastConditions.length} resolved`}>
            {pastConditions.map((c) => (
              <li key={c.id}><span>{c.name}<span className="muted small">
                {c.onset ? dateLabel(c.onset) : ""}{c.resolved ? ` to ${dateLabel(c.resolved)}` : ""}</span></span></li>
            ))}
          </Past>
        )}
      </Section>

      <Section title="Medications" count={activeMeds.length}
        add={editable ? (done) => <AddMedication shell={shell} patientId={pid} done={done} /> : undefined}>
        {activeMeds.length === 0 ? <p className="muted">No current medications.</p> : (
          <ul className="items">
            {activeMeds.map((m) => (
              <li key={m.id}>
                <MedText m={m} />
                {editable && <button className="link small" onClick={() => shell.run(() => api.doctor.stopMedication(m.id),
                  "Medication stopped.")}>Stop</button>}
              </li>
            ))}
          </ul>
        )}
        {pastMeds.length > 0 && (
          <Past label={`${pastMeds.length} past`}>
            {pastMeds.map((m) => <li key={m.id}><MedText m={m} /></li>)}
          </Past>
        )}
      </Section>

      <Section title="Vital signs" count={chart.vitals.length ? undefined : 0}>
        <VitalsTable rows={chart.vitals} />
      </Section>

      <AccessLog shell={shell} patientId={editable ? pid : undefined} you={you} />
      <p className="muted small chart-source">
        {chart.allergies.some((a) => a.source === "synthea") || chart.conditions.some((c) => c.source === "synthea")
          ? "History before CareQueue is synthetic, generated by Synthea. " : ""}
        {editable ? "The chart is shared by the clinic: every doctor who sees this patient sees the same allergies and medications." : ""}
      </p>
    </div>
  );
}

const by = (who: string | null) => (who ? ` · added by ${who}` : "");

function AllergyText({ a }: { a: ChartAllergy }) {
  return (
    <span>
      <span><b>{a.substance}</b>{a.criticality === "high" && <span className="tag risk">High risk</span>}</span>
      <span className="muted small">
        {[a.reaction, a.category].filter(Boolean).join(" · ")}{by(a.recorded_by)}
      </span>
    </span>
  );
}

function MedText({ m }: { m: ChartMedication }) {
  return (
    <span>
      <b>{m.name}</b>
      <span className="muted small">
        {[m.dosage, m.started && `since ${dateLabel(m.started)}`, m.stopped && `stopped ${dateLabel(m.stopped)}`]
          .filter(Boolean).join(" · ")}{by(m.prescribed_by)}
      </span>
    </span>
  );
}

function Section({ title, count, add, children }: {
  title: string; count?: number; add?: (done: () => void) => ReactNode; children: ReactNode;
}) {
  const [adding, setAdding] = useState(false);
  return (
    <section className="chart-sec">
      <div className="chart-head">
        <h3>{title}{count !== undefined && count > 0 && <span className="count"> {count}</span>}</h3>
        {add && !adding && <button className="link small" onClick={() => setAdding(true)}>+ Add</button>}
      </div>
      {adding && add?.(() => setAdding(false))}
      {children}
    </section>
  );
}

function Past({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="past">
      <summary>{label}</summary>
      <ul className="items muted">{children}</ul>
    </details>
  );
}

function AddAllergy({ shell, patientId, done }: { shell: Shell; patientId: number; done: () => void }) {
  const [form, setForm] = useState({ substance: "", reaction: "", criticality: "", category: "" });
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await shell.run(() => api.doctor.addAllergy(patientId, {
      substance: form.substance, reaction: form.reaction, criticality: form.criticality || null, category: form.category || null,
    }), `${form.substance} added to allergies.`);
    if (r) done();
  }
  return (
    <form className="chart-form" onSubmit={submit}>
      <input required autoFocus placeholder="Allergic to, e.g. Penicillin" maxLength={200} value={form.substance}
        onChange={(e) => setForm({ ...form, substance: e.target.value })} aria-label="Allergic to" />
      <input placeholder="Reaction, e.g. Hives" maxLength={200} value={form.reaction}
        onChange={(e) => setForm({ ...form, reaction: e.target.value })} aria-label="Reaction" />
      <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} aria-label="Type">
        <option value="">Type</option><option value="medication">Medication</option><option value="food">Food</option>
        <option value="environment">Environment</option>
      </select>
      <select value={form.criticality} onChange={(e) => setForm({ ...form, criticality: e.target.value })} aria-label="Risk">
        <option value="">Risk</option><option value="low">Low</option><option value="high">High</option>
      </select>
      <div className="row"><button className="primary small-btn">Add</button><button type="button" className="link" onClick={done}>Cancel</button></div>
    </form>
  );
}

function AddMedication({ shell, patientId, done }: { shell: Shell; patientId: number; done: () => void }) {
  const [name, setName] = useState("");
  const [dosage, setDosage] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await shell.run(() => api.doctor.addMedication(patientId, name, dosage),
      (res) => (res.warnings.length ? `Added, but check this: ${res.warnings[0]}` : `${name} added.`));
    if (r) done();
  }
  return (
    <form className="chart-form" onSubmit={submit}>
      <input required autoFocus placeholder="Medication, e.g. Amoxicillin 500 MG Oral Capsule" maxLength={300} value={name}
        onChange={(e) => setName(e.target.value)} aria-label="Medication" />
      <input placeholder="Dosage, e.g. 1 capsule 3 times a day" maxLength={200} value={dosage}
        onChange={(e) => setDosage(e.target.value)} aria-label="Dosage" />
      <div className="row"><button className="primary small-btn">Add</button><button type="button" className="link" onClick={done}>Cancel</button></div>
    </form>
  );
}

function AddOne({ shell, label, placeholder, save, done }: {
  shell: Shell; label: string; placeholder: string; save: (v: string) => Promise<unknown>; done: () => void;
}) {
  const [value, setValue] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await shell.run(() => save(value), `${value} added.`);
    if (r) done();
  }
  return (
    <form className="chart-form" onSubmit={submit}>
      <input required autoFocus placeholder={placeholder} maxLength={200} value={value} onChange={(e) => setValue(e.target.value)} aria-label={label} />
      <div className="row"><button className="primary small-btn">Add</button><button type="button" className="link" onClick={done}>Cancel</button></div>
    </form>
  );
}

/** Blood pressure at or above 140/90 is marked, so a high reading stands out. */
const highBp = (v: VitalsRow) => (v.systolic ?? 0) >= 140 || (v.diastolic ?? 0) >= 90;
const n = (x: number | null, unit = "") => (x === null ? "–" : `${Math.round(x * 10) / 10}${unit}`);

export function VitalsTable({ rows }: { rows: VitalsRow[] }) {
  if (!rows.length) return <p className="muted">No readings yet.</p>;
  return (
    <div className="vitals-wrap">
      <table className="vitals">
        <thead><tr><th>Date</th><th>Blood pressure</th><th>Heart rate</th><th>Temp</th><th>Weight</th><th>BMI</th></tr></thead>
        <tbody>
          {rows.map((v) => (
            <tr key={v.id}>
              <td>{dateLabel(v.taken_at)}</td>
              <td className={highBp(v) ? "hi" : ""}>
                {v.systolic !== null && v.diastolic !== null ? `${n(v.systolic)}/${n(v.diastolic)}` : "–"}
                {highBp(v) && <span className="sr-only"> (high)</span>}
              </td>
              <td>{n(v.heart_rate)}</td>
              <td>{v.temperature_c === null ? "–" : `${n(v.temperature_c)} °C`}</td>
              <td>{v.weight_kg === null ? "–" : `${n(v.weight_kg)} kg`}</td>
              <td>{n(v.bmi)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ACTIONS: Record<string, string> = {
  view_chart: "opened the chart", fhir_read: "read the record through the FHIR API", add_allergy: "added an allergy",
  allergy_inactive: "marked an allergy inactive", allergy_active: "made an allergy active", add_condition: "added a condition",
  resolve_condition: "resolved a condition", add_medication: "added a medication", stop_medication: "stopped a medication",
  record_vitals: "recorded vital signs",
};

function AccessLog({ shell, patientId, you }: { shell: Shell; patientId?: number; you: boolean }) {
  const [open, setOpen] = useState(false);
  const [log] = useData<AccessEntry[] | null>(
    () => (open ? (patientId ? api.doctor.accessLog(patientId) : api.patient.accessLog()) : Promise.resolve(null)),
    [open, patientId, shell.version]);
  return (
    <details className="past access" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>{you ? "Who has looked at your chart" : "Access log"}</summary>
      {!log ? <p className="muted small">Loading…</p> : log.length === 0 ? <p className="muted small">No one yet.</p> : (
        <ul className="items">
          {log.map((e) => (
            <li key={e.id}><span>
              <b>{e.role === "patient" ? (you ? "You" : e.actor) : e.actor}</b> {ACTIONS[e.action] ?? e.action}
              {e.detail ? `: ${e.detail}` : ""}
              <span className="muted small">{relative(e.at, shell.now())}</span>
            </span></li>
          ))}
        </ul>
      )}
    </details>
  );
}
