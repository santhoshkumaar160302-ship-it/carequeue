import { useState } from "react";
import type { Shell } from "./App";
import { AiMeta, api, NoteCheck, Summary, SummaryLine, VitalsInput } from "./api";

/** Where an answer came from, in plain words: which model, or why only the basic version ran. */
function AiNote({ ai, fallback }: { ai: AiMeta; fallback: string }) {
  if (ai.used) {
    const dropped = Array.isArray(ai.dropped) ? ai.dropped.length : ai.dropped ?? 0;
    return (
      <p className="ai-note">
        <span className="tag ai">AI</span> {ai.model ?? "NexusAI"}{ai.ms ? `, ${(ai.ms / 1000).toFixed(1)} s` : ""}
        {dropped > 0 ? ` · ${dropped} unsupported ${dropped === 1 ? "item" : "items"} removed` : ""}
      </p>
    );
  }
  return <p className="ai-note muted">{fallback}{ai.error ? " (the AI wasn't available)" : ""}</p>;
}

// ============================================================ note check
export function NoteChecker({ shell, appointmentId, note, vitals, onResult }: {
  shell: Shell; appointmentId: number; note: string; vitals?: VitalsInput; onResult?: (r: NoteCheck | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    const r = await shell.run(() => api.doctor.checkNote(appointmentId, note, vitals));
    setBusy(false);
    onResult?.(r ?? null);
  }
  return (
    <button type="button" className="ghost small-btn" onClick={run} disabled={busy || !note.trim()}
      title="Look for typos, allergy conflicts, contradictions and missing follow-ups before saving">
      {busy ? "Checking…" : "Check note"}
    </button>
  );
}

export function NoteFindings({ result }: { result: NoteCheck }) {
  const { findings, ai } = result;
  return (
    <div className="findings" aria-live="polite">
      {findings.length === 0 ? <p className="ok-line">No problems found.</p> : (
        <ul>
          {findings.map((f, i) => (
            <li key={i} className={f.severity}>
              <span className="f-icon" aria-hidden="true">{f.severity === "warning" ? "!" : "i"}</span>
              <span>
                {f.message}
                {f.quote && <span className="f-quote">“{f.quote}”</span>}
                {f.source === "ai" && <span className="tag ai">AI</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
      <AiNote ai={ai} fallback="Basic checks only" />
      <p className="muted small">These are suggestions. The note is saved exactly as you wrote it.</p>
    </div>
  );
}

// ============================================================ summary
function Line({ line }: { line: SummaryLine }) {
  const [open, setOpen] = useState<string | null>(null);
  const shown = line.sources.find((s) => s.id === open);
  return (
    <li>
      <span>{line.text}</span>
      <span className="cites">
        {line.sources.map((s) => (
          <button key={s.id} className={`cite ${open === s.id ? "on" : ""}`} title={s.text} aria-expanded={open === s.id}
            onClick={() => setOpen(open === s.id ? null : s.id)}>{s.id}</button>
        ))}
      </span>
      {shown && <span className="cite-text">{shown.text}</span>}
    </li>
  );
}

export function SummaryCard({ shell, patientId, compact }: { shell: Shell; patientId: number; compact?: boolean }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [busy, setBusy] = useState(false);
  async function load() {
    setBusy(true);
    const r = await shell.run(() => api.doctor.summary(patientId));
    setBusy(false);
    if (r) setSummary(r);
  }
  if (!summary) {
    return (
      <div className={`summary-cta ${compact ? "compact" : ""}`}>
        <button className="ghost small-btn" onClick={load} disabled={busy}>{busy ? "Summarizing…" : "Summarize history"}</button>
        {!compact && <span className="muted small">A short brief from the chart and your past notes, with every line linked to its source.</span>}
      </div>
    );
  }
  return (
    <div className="summary">
      <div className="summary-head">
        <h3>Summary</h3>
        <button className="link small" onClick={load} disabled={busy}>{busy ? "…" : "Refresh"}</button>
      </div>
      {summary.kind === "empty" ? <p className="muted">Nothing on record yet.</p> : (
        <ul className="sum-lines">{summary.lines.map((l, i) => <Line key={i} line={l} />)}</ul>
      )}
      {summary.open_items.length > 0 && (
        <>
          <p className="open-label">Needs attention</p>
          <ul className="sum-lines open">{summary.open_items.map((l, i) => <Line key={i} line={l} />)}</ul>
        </>
      )}
      <AiNote ai={summary.ai} fallback="Basic summary, built straight from the record" />
      <p className="muted small">Click a source (A1, V2, N1…) to see the record it came from.</p>
    </div>
  );
}
