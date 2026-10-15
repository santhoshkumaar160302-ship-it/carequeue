import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, Health, initials, Provider, toISODate, User } from "./api";
import { DoctorPortal, DOCTOR_TABS } from "./Doctor";
import { Login } from "./Login";
import { PatientPortal, PATIENT_TABS } from "./Patient";

export type Shell = {
  user: User;
  providers: Provider[];
  health: Health | null;
  version: number; // bumps every few seconds and after every change, so views refetch
  refresh: () => void;
  run: <T>(fn: () => Promise<T>, success?: string | ((r: T) => string)) => Promise<T | undefined>;
  now: () => Date; // the clinic's clock, which can differ from this browser's
  today: string;
  tab: string;
  go: (tab: string, param?: string | number) => void;
  param: string | null;
};

type Theme = "system" | "light" | "dark";
const store = {
  get: (k: string) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string | null) => {
    try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* storage unavailable */ }
  },
};

/** "#/book/12" -> ["book", "12"]. Tabs live in the URL so Back works and links can be shared. */
function readHash(): [string, string | null] {
  const [tab = "", param = null] = window.location.hash.replace(/^#\/?/, "").split("/");
  return [tab, param];
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined); // undefined = still checking
  const [providers, setProviders] = useState<Provider[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [toast, setToast] = useState<{ text: string; kind: "ok" | "error"; id: number } | null>(null);
  const [theme, setTheme] = useState<Theme>(() => (store.get("cq.theme") as Theme) || "system");
  const [[tab, param], setRoute] = useState(readHash);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);
  const notify = useCallback((text: string, kind: "ok" | "error" = "ok") => setToast({ text, kind, id: Date.now() }), []);
  const now = useCallback(() => new Date(Date.now() + offset), [offset]);

  useEffect(() => {
    api.health().then((h) => { setHealth(h); setOffset(new Date(h.time).getTime() - Date.now()); }).catch(() => {});
    api.providers().then(setProviders).catch(() => {});
    api.me().then(setUser).catch(() => setUser(null));
  }, []);
  useEffect(() => {
    const onHash = () => setRoute(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => { if (!user) return; const t = setInterval(refresh, 4000); return () => clearInterval(t); }, [refresh, user]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(t);
  }, [toast]);
  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    store.set("cq.theme", theme === "system" ? null : theme);
  }, [theme]);

  const run = useCallback(async <T,>(fn: () => Promise<T>, success?: string | ((r: T) => string)) => {
    try {
      const result = await fn();
      if (success) notify(typeof success === "function" ? success(result) : success);
      return result;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { setUser(null); notify("Your session ended. Please sign in again.", "error"); }
      else notify(e instanceof ApiError ? e.message : String(e), "error");
      return undefined;
    } finally {
      refresh();
    }
  }, [notify, refresh]);

  const go = useCallback((t: string, p?: string | number) => {
    const hash = `#/${t}${p !== undefined ? `/${p}` : ""}`;
    if (window.location.hash !== hash) window.location.hash = hash;
    else setRoute(readHash());
    window.scrollTo({ top: 0 });
  }, []);

  const tabs = user?.role === "doctor" ? DOCTOR_TABS : PATIENT_TABS;
  const activeTab = tabs.some((t) => t.id === tab) ? tab : tabs[0].id;
  const shell = useMemo<Shell | null>(() => user ? {
    user, providers, health, version, refresh, run, now, today: toISODate(now()), tab: activeTab, go, param,
  } : null, [user, providers, health, version, refresh, run, now, activeTab, go, param]);

  async function signOut() {
    await api.logout().catch(() => {});
    setUser(null);
    window.location.hash = "";
  }
  const nextTheme: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

  return (
    <div className="app">
      <header className="top">
        <a className="brand" href="#/" aria-label="CareQueue home">
          <svg viewBox="0 0 30 30" aria-hidden="true"><path className="stub" d="M3 7a3 3 0 0 1 3-3h18a3 3 0 0 1 3 3v4a4 4 0 0 0 0 8v4a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3v-4a4 4 0 0 0 0-8z" /><path className="cross" d="M11 15h8M15 11v8" /></svg>
          <strong>CareQueue</strong>
          {user && <span className="role-tag">{user.role === "doctor" ? "Doctor" : "Patient"}</span>}
        </a>
        {shell && (
          <nav className="tabs" aria-label="Sections">
            {tabs.map((t) => (
              <a key={t.id} href={`#/${t.id}`} className={t.id === activeTab ? "on" : ""} aria-current={t.id === activeTab ? "page" : undefined}>
                {t.label}
              </a>
            ))}
          </nav>
        )}
        <div className="right">
          {user && (
            <div className="who">
              <span className="avatar" aria-hidden="true">{initials(user.name)}</span>
              <span className="who-name">{user.name}</span>
              <button className="link" onClick={signOut}>Sign out</button>
            </div>
          )}
          <button className="icon-btn" onClick={() => setTheme(nextTheme[theme])}
            title={`Theme: ${theme === "system" ? "matches your system" : theme}. Click to change.`} aria-label={`Theme: ${theme}`}>
            {theme === "system" && <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></svg>}
            {theme === "light" && <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>}
            {theme === "dark" && <svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>}
          </button>
        </div>
      </header>

      {user === undefined && <div className="fatal muted">Opening the clinic…</div>}
      {user === null && <Login health={health} onSignedIn={(u) => { setUser(u); refresh(); window.location.hash = "#/"; }} />}
      {shell && (shell.user.role === "doctor" ? <DoctorPortal shell={shell} /> : <PatientPortal shell={shell} />)}

      {toast && <div key={toast.id} className={`toast ${toast.kind}`} role="status">{toast.text}</div>}
    </div>
  );
}
