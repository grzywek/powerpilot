// The Tarcza card's words for PPE 1's bind probability (ADR 003): the plan
// gave November up on 2026-11-02 on one averaged path; the card now says in
// how many of the four replayed real weeks the cap would bind.
import { test } from "node:test";
import assert from "node:assert/strict";
import { capOddsText, capWeeksLine, type CapScenario } from "../src/cap-odds.ts";

function weeks(binding: number): CapScenario[] {
  return [1, 2, 3, 4].map((week) => ({
    week,
    week_start: ["2026-10-27", "2026-10-20", "2026-10-13", "2026-10-06"][week - 1],
    average_kwh: week <= binding ? 0.7 + week / 100 : 0.5 + week / 100,
    binds: week <= binding,
    dropped_reason: null,
  }));
}

test("no binding week says the limit does not bite", () => {
  assert.equal(capOddsText(weeks(0)), "limit nie działa w żadnym z 4 ostatnich tygodni");
});

test("two binding weeks of four", () => {
  assert.equal(capOddsText(weeks(2)), "limit działa w 2 z 4 ostatnich tygodni");
});

test("every week binds", () => {
  assert.equal(capOddsText(weeks(4)), "limit działa w 4 z 4 ostatnich tygodni");
});

test("a dropped week is not counted and says why", () => {
  const scenarios = weeks(2);
  scenarios[3] = { ...scenarios[3], average_kwh: null, binds: null, dropped_reason: "brak cen" };
  assert.equal(capOddsText(scenarios), "limit działa w 2 z 3 ostatnich tygodni");
  assert.equal(
    capWeeksLine(scenarios),
    "od 27.10: 0.710 (limit) · od 20.10: 0.720 (limit) · od 13.10: 0.530 · od 06.10: pominięty (brak cen)",
  );
});

test("without a usable week there is nothing to say", () => {
  const scenarios = weeks(0).map((s) => ({ ...s, dropped_reason: "brak cen" }));
  assert.equal(capOddsText(scenarios), null);
  assert.equal(capOddsText([]), null);
});
