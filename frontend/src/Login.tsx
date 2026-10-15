import { FormEvent, useState } from "react";
import { api, ApiError, Health, User } from "./api";

export function Login({ health, onSignedIn }: { health: Health | null; onSignedIn: (u: User) => void }) {
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [form, setForm] = useState({ name: "", email: "", phone: "", password: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function attempt(key: string, fn: () => Promise<User>) {
    setBusy(key);
    setError(null);
    try {
      onSignedIn(await fn());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    if (mode === "signin") attempt("form", () => api.login(form.email, form.password));
    else attempt("form", () => api.register(form));
  }
  const field = (k: keyof typeof form) => ({
    value: form[k], onChange: (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value }),
  });
  const demo = health?.demo_logins !== false;

  return (
    <main className="login">
      <section className="login-intro">
        <h1>Appointments that don't go to waste.</h1>
        <p>When someone cancels, CareQueue offers the slot to the next patient waiting, and gives them 15 minutes to take it.
          Patients see their visits and history. Doctors see their day, set their hours, and keep notes.</p>
        {demo && (
          <div className="demo-pick">
            <p className="kicker">Try the demo, no sign-up needed</p>
            <button className="demo-card" disabled={!!busy} onClick={() => attempt("patient", () => api.demo("patient"))}>
              <span className="avatar">HL</span>
              <span><b>I'm a patient</b><span className="muted">Harper Lopez: book, reschedule, see your history</span></span>
              <span aria-hidden="true" className="arrow">{busy === "patient" ? "…" : "→"}</span>
            </button>
            <button className="demo-card" disabled={!!busy} onClick={() => attempt("doctor", () => api.demo("doctor"))}>
              <span className="avatar">MP</span>
              <span><b>I'm a doctor</b><span className="muted">Dr. Maya Patel: your day, your hours, your patients</span></span>
              <span aria-hidden="true" className="arrow">{busy === "doctor" ? "…" : "→"}</span>
            </button>
            <p className="muted small">Everyone in the demo is made up. Sign in as one, then the other, to see a change from both sides.</p>
          </div>
        )}
      </section>

      <section className="login-card" aria-label={mode === "signin" ? "Sign in" : "Create an account"}>
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={mode === "signin"} className={mode === "signin" ? "on" : ""}
            onClick={() => { setMode("signin"); setError(null); }}>Sign in</button>
          <button role="tab" aria-selected={mode === "register"} className={mode === "register" ? "on" : ""}
            onClick={() => { setMode("register"); setError(null); }}>New patient</button>
        </div>
        <form onSubmit={submit} className="form">
          {mode === "register" && (
            <>
              <label>Full name<input required autoComplete="name" maxLength={100} {...field("name")} /></label>
              <label>Phone <span className="muted">(optional)</span><input autoComplete="tel" maxLength={30} {...field("phone")} /></label>
            </>
          )}
          <label>Email<input required type="email" autoComplete="email" maxLength={200} {...field("email")} /></label>
          <label>Password
            <input required type="password" minLength={mode === "register" ? 8 : undefined} maxLength={200}
              autoComplete={mode === "register" ? "new-password" : "current-password"} {...field("password")} />
          </label>
          {mode === "register" && <p className="muted small">At least 8 characters. Doctor accounts are set up by the clinic.</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary wide" disabled={!!busy}>
            {busy === "form" ? "…" : mode === "signin" ? "Sign in" : "Create my account"}
          </button>
        </form>
      </section>
    </main>
  );
}
