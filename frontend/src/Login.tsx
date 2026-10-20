import { FormEvent, KeyboardEvent, useState } from "react";
import { api, ApiError, toISODate, User } from "./api";

type Role = "patient" | "doctor";
type Mode = "signin" | "register";

const ROLE_KEY = "cq.signin.role";
function savedRole(): Role {
  try { return localStorage.getItem(ROLE_KEY) === "doctor" ? "doctor" : "patient"; } catch { return "patient"; }
}

const EMPTY = { name: "", email: "", phone: "", birth_date: "", sex: "", password: "", confirm: "", consent: false };

/** Sign in as a patient or a doctor, or create a patient account. Doctor accounts are made by the clinic. */
export function Login({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [role, setRoleState] = useState<Role>(savedRole);
  const [mode, setMode] = useState<Mode>("signin");
  const [form, setForm] = useState(EMPTY);
  const [showPw, setShowPw] = useState(false);
  const [caps, setCaps] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const setRole = (r: Role) => {
    setRoleState(r);
    setError(null);
    try { localStorage.setItem(ROLE_KEY, r); } catch { /* private mode: nothing to remember */ }
  };
  const switchMode = (m: Mode) => { setMode(m); setError(null); setShowPw(false); };
  const field = (k: "name" | "email" | "phone" | "birth_date" | "sex" | "password" | "confirm") => ({
    value: form[k], onChange: (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value }),
  });
  const watchCaps = (e: KeyboardEvent<HTMLInputElement>) => setCaps(e.getModifierState?.("CapsLock") ?? false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "register" && form.password !== form.confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const user = mode === "signin"
        ? await api.login(form.email, form.password, role)
        : await api.register({ name: form.name, email: form.email, phone: form.phone, password: form.password,
          birth_date: form.birth_date || null, sex: form.sex || null });
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const passwordField = (id: string, label: string, k: "password" | "confirm", autoComplete: string, hint?: string) => (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="pw">
        <input id={id} required type={showPw ? "text" : "password"} minLength={mode === "register" ? 8 : undefined}
          maxLength={200} autoComplete={autoComplete} onKeyUp={watchCaps} onBlur={() => setCaps(false)}
          aria-describedby={hint ? `${id}-hint` : undefined} {...field(k)} />
        {k === "password" && (
          <button type="button" className="pw-toggle" onClick={() => setShowPw(!showPw)}
            aria-label={showPw ? "Hide password" : "Show password"} aria-pressed={showPw}>
            {showPw
              ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.9 5.2A10 10 0 0112 5c6.5 0 10 7 10 7a17 17 0 01-3.2 4.2M6.1 6.1C3.6 7.8 2 12 2 12s3.5 7 10 7a9.7 9.7 0 004.1-.9" /></svg>
              : <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>}
          </button>
        )}
      </div>
      {hint && <p id={`${id}-hint`} className="hint">{hint}</p>}
      {k === "password" && caps && (
        <p className="caps" role="status">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z" /></svg>
          Caps Lock is on
        </p>
      )}
    </div>
  );

  return (
    <main className="signin">
      <form className="signin-card" onSubmit={submit} noValidate={false}>
        <div className="signin-head">
          <h1>{mode === "signin" ? "Sign in to CareQueue" : "Create your patient account"}</h1>
          <p>{mode === "signin" ? "First, tell us who you are." : "Book visits, join waitlists and see your health record."}</p>
        </div>

        {mode === "signin" && (
          <div className="roles" role="radiogroup" aria-label="I am a">
            <button type="button" role="radio" aria-checked={role === "patient"} className={role === "patient" ? "on" : ""}
              onClick={() => setRole("patient")}>
              <span className="role-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 4-7 8-7s8 3 8 7" /></svg>
              </span>
              <b>I'm a patient</b>
              <span>Book visits, join waitlists, see your health record.</span>
            </button>
            <button type="button" role="radio" aria-checked={role === "doctor"} className={role === "doctor" ? "on" : ""}
              onClick={() => setRole("doctor")}>
              <span className="role-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><path d="M6 3v6a4 4 0 008 0V3" /><path d="M10 13v3a5 5 0 0010 0v-2" /><circle cx="20" cy="12" r="2" /></svg>
              </span>
              <b>I'm a doctor</b>
              <span>Your day, your shifts, your patients' charts.</span>
            </button>
          </div>
        )}

        {mode === "register" && (
          <>
            <div className="field"><label htmlFor="su-name">Full name</label>
              <input id="su-name" required autoComplete="name" maxLength={100} {...field("name")} /></div>
          </>
        )}

        <div className="field"><label htmlFor="si-email">{role === "doctor" && mode === "signin" ? "Work email" : "Email"}</label>
          <input id="si-email" required type="email" autoComplete="email" maxLength={200} autoFocus
            placeholder="you@example.com" {...field("email")} /></div>

        {mode === "register" && (
          <>
            <div className="field"><label htmlFor="su-phone">Phone <span className="optional">(optional)</span></label>
              <input id="su-phone" type="tel" autoComplete="tel" maxLength={30} {...field("phone")} /></div>
            <div className="two">
              <div className="field"><label htmlFor="su-dob">Date of birth</label>
                <input id="su-dob" required type="date" autoComplete="bday" min="1906-01-01" max={toISODate(new Date())}
                  {...field("birth_date")} /></div>
              <div className="field"><label htmlFor="su-sex">Sex</label>
                <select id="su-sex" required {...field("sex")}>
                  <option value="" disabled>Choose…</option>
                  <option value="female">Female</option>
                  <option value="male">Male</option>
                  <option value="other">Other</option>
                  <option value="unknown">Prefer not to say</option>
                </select></div>
            </div>
          </>
        )}

        {passwordField("si-password", "Password", "password", mode === "signin" ? "current-password" : "new-password",
          mode === "register" ? "At least 8 characters." : undefined)}
        {mode === "register" && passwordField("su-confirm", "Confirm password", "confirm", "new-password")}

        {mode === "register" && (
          <label className="consent">
            <input type="checkbox" required checked={form.consent} onChange={(e) => setForm({ ...form, consent: e.target.checked })} />
            <span>I understand this is a demo clinic and I won't enter real medical information.</span>
          </label>
        )}

        {error && <p className="form-error" role="alert">{error}</p>}

        <button className="primary signin-submit" disabled={busy}>
          {busy ? "One moment…" : mode === "register" ? "Create my account" : `Sign in as a ${role}`}
        </button>

        <p className="signin-foot">
          {mode === "register"
            ? <>Already have an account? <button type="button" className="link" onClick={() => switchMode("signin")}>Sign in</button></>
            : role === "patient"
              ? <>No account yet? <button type="button" className="link" onClick={() => switchMode("register")}>Create one</button></>
              : <>Doctor accounts are set up by your clinic's management.</>}
        </p>
      </form>
      <p className="signin-note">Demo clinic. Please don't enter real medical information.</p>
    </main>
  );
}
