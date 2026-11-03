/**
 * How the Tarcza card says how often PPE 1's month binds (ADR 003).
 *
 * The plan judges PPE 1's month on the four latest real price weeks replayed
 * over the rest of the month and prices its kWh by the share in which the cap
 * binds. The card says that share in words — "limit działa w 2 z 4 ostatnich
 * tygodni" — and lists each week's month average on one muted line, the
 * binding ones marked, a dropped week with its reason.
 */

/** One replayed week as the status payload carries it (``CapScenario``). */
export interface CapScenario {
  week: number;
  week_start: string | null;
  average_kwh: number | null;
  binds: boolean | null;
  dropped_reason: string | null;
}

/** "limit działa w 2 z 4 ostatnich tygodni"; ``null`` without any week. */
export function capOddsText(scenarios: CapScenario[]): string | null {
  const usable = scenarios.filter((s) => s.dropped_reason == null);
  if (!usable.length) return null;
  const binding = usable.filter((s) => s.binds).length;
  if (binding === 0) return `limit nie działa w żadnym z ${usable.length} ostatnich tygodni`;
  return `limit działa w ${binding} z ${usable.length} ostatnich tygodni`;
}

function dayMonth(iso: string | null): string {
  if (!iso) return "—";
  const [, month, day] = iso.split("-");
  return `${day}.${month}`;
}

/** "od 27.10: 0.766 (limit) · od 20.10: 0.523 · …", latest week first. */
export function capWeeksLine(scenarios: CapScenario[]): string {
  return [...scenarios]
    .sort((a, b) => a.week - b.week)
    .map((s) => {
      const label = `od ${dayMonth(s.week_start)}`;
      if (s.dropped_reason != null) return `${label}: pominięty (${s.dropped_reason})`;
      const avg = s.average_kwh != null ? s.average_kwh.toFixed(3) : "—";
      return `${label}: ${avg}${s.binds ? " (limit)" : ""}`;
    })
    .join(" · ");
}
