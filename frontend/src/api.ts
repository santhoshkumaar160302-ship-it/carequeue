// Typed client for the CareQueue API. The session lives in an HttpOnly cookie, so there's no token here.

export type Provider = { id: number; name: string; specialty: string };
export type User =
  | { id: number; email: string; role: "patient"; patient_id: number; name: string }
  | { id: number; email: string; role: "doctor"; provider_id: number; name: string; specialty: string };

export type SlotState = "open" | "booked" | "held" | "blocked" | "completed" | "no_show"
  | "mine" | "offer" | "taken" | "unavailable";
export type Slot = {
  id: number; start: string; end: string; state: SlotState; patient_id: number | null; appointment_id: number | null;
  past: boolean; blocked_reason?: "hours" | "time_off" | null; patient?: string | null;
};
export type Schedule = { provider_id: number; date: string; source: "cache" | "database"; slots: Slot[] };
export type Week = { provider_id: number; start: string; days: Schedule[] };

export type Visit = {
  id: number; status: "booked" | "held" | "completed" | "no_show" | "cancelled"; source: string; slot_id: number;
  start: string; end: string; label: string; note: string | null; cancel_reason: string | null;
  hold_expires_at: string | null; provider_id?: number; provider?: string; specialty?: string;
  patient_id?: number; patient?: string; phone?: string;
};
export type MyWaitlist = {
  id: number; status: "waiting" | "offered"; provider: string | null; specialty: string | null; earliest: string;
  latest: string; time_of_day: string; priority: number; replaces_appointment_id: number | null; position: number | null;
};
export type Notice = { id: number; kind: string; message: string; at: string };
export type Overview = {
  patient: { id: number; name: string; phone: string };
  next: Visit | null; upcoming: Visit[]; offers: Visit[]; last_visit: Visit | null;
  waitlist: MyWaitlist[]; notifications: Notice[];
};

export type TodayVisit = Visit & { past: boolean; now: boolean };

// ---------- the patient chart ----------
export type ChartBrief = { allergies: string[]; conditions: string[]; medications: string[] };
export type ChartAllergy = {
  id: number; substance: string; code: string | null; category: string | null; criticality: string | null;
  reaction: string | null; status: "active" | "inactive"; recorded_on: string | null; recorded_by: string | null; source: string;
};
export type ChartCondition = {
  id: number; name: string; code: string | null; status: "active" | "resolved"; onset: string | null; resolved: string | null;
  recorded_by: string | null; source: string;
};
export type ChartMedication = {
  id: number; name: string; code: string | null; dosage: string | null; status: "active" | "stopped"; started: string | null;
  stopped: string | null; prescribed_by: string | null; source: string;
};
export type VitalsRow = {
  id: number; taken_at: string; label: string; appointment_id: number | null; systolic: number | null; diastolic: number | null;
  heart_rate: number | null; temperature_c: number | null; weight_kg: number | null; height_cm: number | null; bmi: number | null;
  source: string;
};
export type VitalsInput = Partial<Record<"systolic" | "diastolic" | "heart_rate" | "temperature_c" | "weight_kg" | "height_cm", number>>;
export type ChartData = {
  patient: { id: number; name: string; sex: string | null; birth_date: string | null; age: number | null };
  allergies: ChartAllergy[]; conditions: ChartCondition[]; medications: ChartMedication[]; vitals: VitalsRow[];
};
export type NoteFinding = {
  kind: string; severity: "warning" | "info"; message: string; quote: string | null; source: "rules" | "ai";
};
export type AiMeta = { used: boolean; model?: string; ms?: number; error?: string; dropped?: number | { text: string; why: string }[] };
export type NoteCheck = { findings: NoteFinding[]; ai: AiMeta };
export type SummarySource = { id: string; kind: string; text: string };
export type SummaryLine = { text: string; sources: SummarySource[] };
export type Summary = { lines: SummaryLine[]; open_items: SummaryLine[]; kind: "ai" | "basic" | "empty"; ai: AiMeta };
export type AccessEntry = { id: number; at: string; actor: string; role: "doctor" | "patient"; action: string; detail: string };
export type Today = {
  provider: Provider; date: string; visits: TodayVisit[]; earlier: TodayVisit[];
  next: (Visit & { now: boolean; last_visit: Visit | null; chart?: ChartBrief }) | null;
  counts: { booked: number; done: number; to_close: number; open_today: number; waitlist: number };
};
/** "mine": this doctor has seen them before. "new": booked or waiting, but not seen by this doctor yet. */
export type PatientGroup = "mine" | "new";
export type DoctorPatient = {
  id: number; name: string; phone: string; visits: number; group: PatientGroup; waiting: boolean;
  last_visit: { start: string; label: string } | null;
  next_visit: { start: string; label: string; appointment_id: number; offer: boolean } | null;
};
export type RecordWaiting = { id: number; status: "waiting" | "offered"; earliest: string; latest: string; time_of_day: string; priority: number };
export type PatientRecord = {
  id: number; name: string; phone: string; group: PatientGroup; visits: number; waiting: RecordWaiting[];
  upcoming: Visit[]; history: Visit[];
};
/** The parts of the day a patient can ask for. Night runs past midnight. */
export const TIME_OF_DAY: { value: string; label: string; hours: string }[] = [
  { value: "any", label: "Any time", hours: "" },
  { value: "morning", label: "Mornings", hours: "6 AM – 12 PM" },
  { value: "afternoon", label: "Afternoons", hours: "12 – 6 PM" },
  { value: "evening", label: "Evenings", hours: "6 – 10 PM" },
  { value: "night", label: "Nights", hours: "10 PM – 6 AM" },
];
/** "evenings (6 – 10 PM)", or "" for any time. */
export const timeOfDayText = (v: string) => {
  const t = TIME_OF_DAY.find((x) => x.value === v);
  return !t || v === "any" ? "" : `${t.label.toLowerCase()} (${t.hours})`;
};
export type ShiftName = "morning" | "day" | "evening" | "night" | "custom";
export type Shift = { name: Exclude<ShiftName, "custom">; label: string; start: string; end: string };
export type HoursDay = {
  weekday: number; name: string; working: boolean; start: string | null; end: string | null;
  shift: ShiftName | null; overnight: boolean;
};
export type TimeOff = { id: number; start: string; end: string; reason: string; label: string };
export type Affected = { appointment_id: number; patient_id: number; patient: string; start: string; label: string };
export type Preview = { blocked: number; opened: number; affected: Affected[] };
export type ChangeResult = { blocked: number; opened: number; rebooking: { patient: string; was: string; offered_appointment_id: number | null }[] };

export type StoryEvent = { type: string; at: string; cause: string | null; patient: string | null; result: string | null };
export type SlotStory = {
  slot: { id: number; start: string; end: string; label: string; provider_id: number; provider: string; specialty: string; past: boolean };
  state: string;
  appointment: { id: number; status: "booked" | "held"; source: string; patient_id: number; patient: string; hold_expires_at: string | null } | null;
  events: StoryEvent[];
  next_in_line: { patient: string; entry_id: number }[];
};
export type WaitlistRow = {
  id: number; patient_id: number; patient: string; status: "waiting" | "offered"; provider: string | null;
  specialty: string | null; earliest: string; latest: string; time_of_day: string; position: number; priority: number;
};
export type Option = { slot_id: number; start: string; provider_id: number; provider: string; specialty: string; label: string };
export type Suggestion = {
  parser: "nexusai" | "rules";
  gateway: { model: string; cache: string; cost_usd: number; latency_ms: number } | null;
  fallback_reason: string | null;
  understood: { provider: string | null; specialty: string | null; earliest: string | null; latest: string | null; time_of_day: string };
  options: Option[];
};
export type ActivityEvent = {
  id: number; type: string; at: string; patient: string | null; slot: string | null; provider: string | null;
  cause: string | null; result: string | null; processed: boolean;
};
export type Health = {
  status: string; version: string; time: string; database: string; cache: string; queue: string; assistant: string;
  demo_logins: boolean;
};

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError("Can't reach the server. Is it running?", 0);
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join("; ") : body.detail;
    throw new ApiError(body.error || detail || `Request failed (${res.status})`, res.status);
  }
  return body as T;
}

const send = <T>(method: string, path: string, data?: unknown) =>
  call<T>(path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const post = <T>(path: string, data?: unknown) => send<T>("POST", path, data);

export const api = {
  health: () => call<Health>("/api/health"),
  providers: () => call<Provider[]>("/api/providers"),

  me: () => call<User>("/api/auth/me"),
  login: (email: string, password: string, role?: "patient" | "doctor") => post<User>("/api/auth/login", { email, password, role }),
  register: (data: { name: string; email: string; phone: string; password: string; birth_date: string | null; sex: string | null }) =>
    post<User>("/api/auth/register", data),
  logout: () => post("/api/auth/logout"),

  patient: {
    overview: () => call<Overview>("/api/me/overview"),
    history: () => call<Visit[]>("/api/me/history"),
    chart: () => call<ChartData>("/api/me/chart"),
    accessLog: () => call<AccessEntry[]>("/api/me/access-log"),
    week: (providerId: number, start: string) => call<Week>(`/api/me/week?provider_id=${providerId}&start=${start}`),
    book: (slotId: number, source = "direct") => post<{ id: number; label: string; provider: string }>("/api/me/appointments", { slot_id: slotId, source }),
    reschedule: (id: number, slotId: number) => post<{ label: string; provider: string }>(`/api/me/appointments/${id}/reschedule`, { slot_id: slotId }),
    cancel: (id: number) => post(`/api/me/appointments/${id}/cancel`),
    accept: (id: number) => post<{ released_appointment_id: number | null }>(`/api/me/appointments/${id}/accept`),
    decline: (id: number) => post(`/api/me/appointments/${id}/decline`),
    joinWaitlist: (data: { earliest: string; latest: string; provider_id?: number | null; specialty?: string | null;
      time_of_day: string; replaces_appointment_id?: number | null }) =>
      post<{ id: number; status: string; position: number; offered_appointment_id: number | null }>("/api/me/waitlist", data),
    leaveWaitlist: (id: number) => send("DELETE", `/api/me/waitlist/${id}`),
    ask: (text: string) => post<Suggestion>("/api/me/assistant", { text }),
  },

  doctor: {
    today: () => call<Today>("/api/doctor/today"),
    week: (start: string) => call<Week>(`/api/doctor/week?start=${start}`),
    slot: (id: number) => call<SlotStory>(`/api/doctor/slots/${id}`),
    complete: (id: number, note?: string, vitals?: VitalsInput) =>
      post<Visit>(`/api/doctor/appointments/${id}/complete`, { note: note ?? null, vitals: vitals ?? null }),
    setVitals: (id: number, vitals: VitalsInput) => send<VitalsRow>("PUT", `/api/doctor/appointments/${id}/vitals`, vitals),
    chart: (patientId: number) => call<ChartData>(`/api/doctor/patients/${patientId}/chart`),
    checkNote: (appointmentId: number, note: string, vitals?: VitalsInput) =>
      post<NoteCheck>(`/api/doctor/appointments/${appointmentId}/check-note`, { note, vitals: vitals ?? null }),
    summary: (patientId: number) => post<Summary>(`/api/doctor/patients/${patientId}/summary`),
    accessLog: (patientId: number) => call<AccessEntry[]>(`/api/doctor/patients/${patientId}/access-log`),
    addAllergy: (patientId: number, data: { substance: string; reaction?: string; criticality?: string | null; category?: string | null }) =>
      post<{ id: number }>(`/api/doctor/patients/${patientId}/allergies`, data),
    allergyStatus: (id: number, status: "active" | "inactive") => post(`/api/doctor/allergies/${id}/status`, { status }),
    addCondition: (patientId: number, name: string) => post<{ id: number }>(`/api/doctor/patients/${patientId}/conditions`, { name }),
    resolveCondition: (id: number) => post(`/api/doctor/conditions/${id}/resolve`),
    addMedication: (patientId: number, name: string, dosage: string) =>
      post<{ id: number; warnings: string[] }>(`/api/doctor/patients/${patientId}/medications`, { name, dosage }),
    stopMedication: (id: number) => post(`/api/doctor/medications/${id}/stop`),
    noShow: (id: number) => post<Visit>(`/api/doctor/appointments/${id}/no-show`),
    note: (id: number, note: string) => send<Visit>("PUT", `/api/doctor/appointments/${id}/note`, { note }),
    cancel: (id: number) => post<{ offered_appointment_id: number | null }>(`/api/doctor/appointments/${id}/cancel`),
    hours: () => call<HoursDay[]>("/api/doctor/hours"),
    shifts: () => call<Shift[]>("/api/doctor/shifts"),
    previewHours: (days: HoursInput[]) => post<Preview>("/api/doctor/hours/preview", { days }),
    setHours: (days: HoursInput[]) => send<ChangeResult & { hours: HoursDay[] }>("PUT", "/api/doctor/hours", { days }),
    timeOff: () => call<TimeOff[]>("/api/doctor/time-off"),
    previewTimeOff: (data: TimeOffInput) => post<Preview>("/api/doctor/time-off/preview", data),
    addTimeOff: (data: TimeOffInput) => post<ChangeResult>("/api/doctor/time-off", data),
    removeTimeOff: (id: number) => send<ChangeResult>("DELETE", `/api/doctor/time-off/${id}`),
    patients: (q = "", group: "all" | PatientGroup = "all") =>
      call<DoctorPatient[]>(`/api/doctor/patients?q=${encodeURIComponent(q)}&group=${group}`),
    patient: (id: number) => call<PatientRecord>(`/api/doctor/patients/${id}`),
    activity: () => call<ActivityEvent[]>("/api/doctor/activity"),
    waitlist: () => call<WaitlistRow[]>("/api/doctor/waitlist"),
  },
};
export type HoursInput = { weekday: number; working: boolean; shift: ShiftName; start: string | null; end: string | null };
export type TimeOffInput = { start: string; end: string; reason: string };

// ---------- formatting helpers ----------
export const toISODate = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
export const parseLocal = (iso: string) => new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
/** Dates and times are always shown US-style with a 12-hour clock ("2:30 PM"), whatever the browser's region. */
export const LOCALE = "en-US";
export const timeLabel = (iso: string) =>
  parseLocal(iso).toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit", hour12: true });
export const dayLabel = (iso: string) =>
  parseLocal(iso).toLocaleDateString(LOCALE, { weekday: "short", month: "short", day: "numeric" });
/** A date that may be years ago: "Mar 4, 2019". */
export const dateLabel = (iso: string) =>
  parseLocal(iso.slice(0, 10)).toLocaleDateString(LOCALE, { month: "short", day: "numeric", year: "numeric" });
export const longDay = (iso: string) =>
  parseLocal(iso).toLocaleDateString(LOCALE, { weekday: "long", month: "long", day: "numeric" });
export const pct = (x: number) => `${Math.round(x * 1000) / 10}%`;

export function mondayOf(d: Date): string {
  const x = new Date(d);
  const dow = x.getDay();
  x.setDate(x.getDate() + (dow === 0 ? 1 : dow === 6 ? 2 : 1 - dow)); // weekends jump to the coming Monday
  return toISODate(x);
}
export function addDays(iso: string, n: number): string {
  const d = parseLocal(iso);
  d.setDate(d.getDate() + n);
  return toISODate(d);
}
export const initials = (name: string) =>
  name.replace("Dr. ", "").split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase();

export function relative(iso: string, now: Date): string {
  const s = Math.round((now.getTime() - parseLocal(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return dayLabel(iso);
}

/** "in 3 days", "tomorrow", "today": how far away a visit is, in plain words. */
export function until(iso: string, now: Date): string {
  const start = parseLocal(iso);
  const days = Math.round((parseLocal(toISODate(start)).getTime() - parseLocal(toISODate(now)).getTime()) / 86400000);
  if (days <= 0) {
    const mins = Math.round((start.getTime() - now.getTime()) / 60000);
    if (mins <= 0) return "now";
    return mins < 60 ? `in ${mins} min` : `today`;
  }
  return days === 1 ? "tomorrow" : `in ${days} days`;
}

export type StatusLabelKey = "booked" | "held" | "completed" | "no_show" | "cancelled";
export const STATUS_LABEL: Record<string, string> = {
  booked: "Booked", held: "Offer", completed: "Seen", no_show: "Missed", cancelled: "Cancelled",
};
export const CANCEL_LABEL: Record<string, string> = {
  patient: "You cancelled", doctor: "The doctor cancelled", doctor_unavailable: "The doctor was unavailable",
  rescheduled: "Moved to another time", moved_up: "Moved to an earlier slot",
};
