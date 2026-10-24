// Run with TZ=Europe/Warsaw (npm test sets it): the dates below are local
// Warsaw times, and 2026-10-25 is the day the clocks go back.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtDay, fmtTime, fmtWhen, fmtWhenRange } from "../src/when.ts";

const NOW = new Date("2026-10-24T15:46:00+02:00");

test("a moment today still says it is today", () => {
  // The bug: "Następne przełączenie: 15:00" with no day at all.
  assert.equal(fmtWhen("2026-10-24T16:00:00+02:00", NOW), "dziś 16:00");
});

test("tomorrow and yesterday are named, not dated", () => {
  assert.equal(fmtWhen("2026-10-25T07:00:00+01:00", NOW), "jutro 07:00");
  assert.equal(fmtWhen("2026-10-23T23:00:00+02:00", NOW), "wczoraj 23:00");
});

test("further days carry the weekday and the date", () => {
  assert.equal(fmtWhen("2026-10-26T21:00:00+01:00", NOW), "pon 26.10 21:00");
  assert.equal(fmtWhen("2026-10-17T17:59:09+02:00", NOW), "sob 17.10 17:59");
});

test("another year adds the year", () => {
  assert.equal(fmtWhen("2025-12-31T08:00:00+01:00", NOW), "śr 31.12.2025 08:00");
});

test("the day the clocks go back is still one day away", () => {
  // 25.10 has 25 hours; a millisecond difference divided by 24 h would put
  // its late evening two days away.
  assert.equal(fmtWhen("2026-10-25T23:30:00+01:00", NOW), "jutro 23:30");
  assert.equal(fmtDay("2026-10-26T00:30:00+01:00", NOW), "pon 26.10");
});

test("a span within one day names the day once", () => {
  assert.equal(
    fmtWhenRange("2026-10-24T15:00:00+02:00", "2026-10-24T16:00:00+02:00", NOW),
    "dziś 15:00–16:00",
  );
  // An end at the following midnight closes the day it started in.
  assert.equal(
    fmtWhenRange("2026-10-24T23:00:00+02:00", "2026-10-25T00:00:00+02:00", NOW),
    "dziś 23:00–00:00",
  );
});

test("a span across days names both days", () => {
  assert.equal(
    fmtWhenRange("2026-10-24T22:00:00+02:00", "2026-10-25T02:00:00+01:00", NOW),
    "dziś 22:00 – jutro 02:00",
  );
});

test("missing or unreadable moments read as a dash", () => {
  assert.equal(fmtWhen(null, NOW), "—");
  assert.equal(fmtWhen("nonsense", NOW), "—");
  assert.equal(fmtTime(undefined), "—");
  assert.equal(fmtWhenRange("2026-10-24T15:00:00+02:00", null, NOW), "—");
});
