import { useEffect, useState } from "react";
import { LOCALE, addDays, parseLocal, Slot, timeLabel, Week, zoneName } from "./api";

const ROW = 46; // px per half-hour row, including the gap

export type CellLook = { text: string; kind: string; label?: string };

type Props = {
  week: Week;
  today: string;
  now: Date;
  selected: number | null;
  onSelect: (slotId: number | null) => void;
  look: (slot: Slot) => CellLook;
  /** Hide rows at the start and end of the day that are unavailable on every day shown. */
  trim?: boolean;
};

export function WeekGrid({ week, today, now, selected, onSelect, look, trim }: Props) {
  const [mobileDay, setMobileDay] = useState(0);

  useEffect(() => {
    // On phones one day shows at a time: start on today if it's in this week.
    const idx = week.days.findIndex((d) => d.date === today);
    setMobileDay(idx >= 0 ? idx : 0);
  }, [week.start, week.provider_id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!week.days.length) return <div className="cal-empty">No clinic days in this range. Bookings open two weeks ahead.</div>;

  const allTimes = week.days[0].slots.map((s) => s.start.slice(11, 16));
  let first = 0;
  let last = allTimes.length - 1;
  if (trim) {
    // Outside everyone's hours on every day shown. Time off stays visible, so the doctor sees it.
    const off = (i: number) => week.days.every((d) => {
      const s = d.slots[i];
      return !s || s.state === "unavailable" || (s.state === "blocked" && s.blocked_reason !== "time_off");
    });
    while (first < last && off(first)) first++;
    while (last > first && off(last)) last--;
  }
  const times = allTimes.slice(first, last + 1);
  const [h0, m0] = times[0].split(":").map(Number);
  const nowTop = ((now.getHours() - h0) * 60 + now.getMinutes() - m0) / 30 * ROW;
  const showNow = nowTop >= 0 && nowTop <= times.length * ROW;

  return (
    <>
      <div className="mobile-days" role="tablist" aria-label="Day">
        {week.days.map((d, i) => (
          <button key={d.date} role="tab" aria-selected={i === mobileDay} className={i === mobileDay ? "on" : ""} onClick={() => setMobileDay(i)}>
            {parseLocal(d.date).toLocaleDateString(LOCALE, { weekday: "short" })} <b>{parseLocal(d.date).getDate()}</b>
          </button>
        ))}
      </div>
      <div className="cal" style={{ ["--row" as string]: `${ROW}px`, ["--days" as string]: week.days.length }}>
        <div className="col times">
          <div className="day-head" />
          {times.map((t) => (
            <div key={t} className="time">{t.endsWith(":00") ? timeLabel(`2000-01-01T${t}`).replace(":00", "") : ""}</div>
          ))}
        </div>
        {week.days.map((day, i) => {
          const isToday = day.date === today;
          return (
            <div key={day.date} className={`col day ${i === mobileDay ? "m-on" : ""}`}>
              <div className={`day-head ${isToday ? "today" : ""}`}>
                <span>{parseLocal(day.date).toLocaleDateString(LOCALE, { weekday: "short" })}</span>
                <b>{parseLocal(day.date).getDate()}</b>
              </div>
              <div className="cells">
                {isToday && showNow && <div className="now" style={{ top: nowTop }} aria-hidden="true" />}
                {day.slots.slice(first, last + 1).map((slot) => {
                  const { text, kind, label } = look(slot);
                  const sel = selected === slot.id;
                  return (
                    <button key={slot.id} className={`cell ${kind} ${sel ? "sel" : ""}`} aria-pressed={sel}
                      aria-label={`${parseLocal(day.date).toLocaleDateString(LOCALE, { weekday: "long" })} ${timeLabel(slot.start)}, ${label ?? (text || kind)}`}
                      onClick={() => onSelect(sel ? null : slot.id)}>
                      <span>{text}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

export function WeekNav({ start, today, onChange }: { start: string; today: string; onChange: (iso: string) => void }) {
  const fmt = (iso: string) => parseLocal(iso).toLocaleDateString(LOCALE, { month: "short", day: "numeric" });
  return (
    <div className="weeknav">
      <button className="icon-btn" aria-label="Previous week" onClick={() => onChange(addDays(start, -7))}>‹</button>
      <span>{fmt(start)} – {fmt(addDays(start, 6))}</span>
      <button className="icon-btn" aria-label="Next week" onClick={() => onChange(addDays(start, 7))}>›</button>
      {start !== today && <button className="link" onClick={() => onChange(today)}>Today</button>}
    </div>
  );
}

export function Legend({ items }: { items: [string, string][] }) {
  return (
    <div className="legend-row">
      <div className="legend" aria-hidden="true">
        {items.map(([k, label]) => <span key={k}><i className={`k-${k}`} />{label}</span>)}
      </div>
      <p className="tz-note">Times are {zoneName("long")} ({zoneName()})</p>
    </div>
  );
}
