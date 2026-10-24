/**
 * How the panel names a moment.
 *
 * A bare "15:00" leaves the reader guessing which day it is: the apartment
 * card once read "Następne przełączenie: 15:00" right next to
 * "17.10 17:59 → 15:00". So every moment carries its day — "dziś 15:00",
 * "jutro 07:00", "wczoraj 23:00", further out "pon 27.10 21:00", with the year
 * when it is not the current one. The panel re-renders every minute, so the
 * relative words never outlive their day on screen; text stored for later
 * (notifications, logs) uses the backend's absolute form instead.
 *
 * Every function takes ``now`` so the tests can pin the reference day; the
 * panel passes nothing and gets the live clock. Local time is the browser's.
 */

const WEEKDAY = ["nd", "pon", "wt", "śr", "czw", "pt", "sob"];

type Moment = Date | string | number | null | undefined;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function toDate(moment: Moment): Date | null {
  if (moment == null || moment === "") return null;
  const d = moment instanceof Date ? moment : new Date(moment);
  return isNaN(d.getTime()) ? null : d;
}

/** Calendar days from ``ref``'s local day to ``d``'s (DST days count as one). */
function dayOffset(d: Date, ref: Date): number {
  const a = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const b = Date.UTC(ref.getFullYear(), ref.getMonth(), ref.getDate());
  return Math.round((a - b) / 86_400_000);
}

/** The day alone: "dziś", "jutro", "wczoraj", otherwise "pon 27.10". */
export function fmtDay(moment: Moment, now: Date = new Date()): string {
  const d = toDate(moment);
  if (!d) return "—";
  const offset = dayOffset(d, now);
  if (offset === 0) return "dziś";
  if (offset === 1) return "jutro";
  if (offset === -1) return "wczoraj";
  const year = d.getFullYear() !== now.getFullYear() ? `.${d.getFullYear()}` : "";
  return `${WEEKDAY[d.getDay()]} ${pad(d.getDate())}.${pad(d.getMonth() + 1)}${year}`;
}

/** The clock alone ("15:00") — only where the day is already on screen. */
export function fmtTime(moment: Moment): string {
  const d = toDate(moment);
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "—";
}

/** A moment with its day: "dziś 15:00", "jutro 07:00", "pon 27.10 21:00". */
export function fmtWhen(moment: Moment, now: Date = new Date()): string {
  const d = toDate(moment);
  return d ? `${fmtDay(d, now)} ${fmtTime(d)}` : "—";
}

/** A span. One day when both ends share it — an end at the following
 *  midnight closes that day, so "dziś 23:00–00:00" — both days otherwise:
 *  "dziś 22:00 – jutro 02:00". */
export function fmtWhenRange(start: Moment, end: Moment, now: Date = new Date()): string {
  const s = toDate(start);
  const e = toDate(end);
  if (!s || !e) return "—";
  const span = dayOffset(e, s);
  const closesDay = span === 1 && e.getHours() === 0 && e.getMinutes() === 0;
  if (span === 0 || closesDay) return `${fmtDay(s, now)} ${fmtTime(s)}–${fmtTime(e)}`;
  return `${fmtWhen(s, now)} – ${fmtWhen(e, now)}`;
}
