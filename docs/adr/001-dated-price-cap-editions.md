# ADR 001: Price cap (Tarcza) as dated editions

Status: accepted, 2026-10-17

## Context

The seller's monthly price cap changes its terms between editions. Tarcza
Pstryk 2026 averages energy plus the handling fee and caps it at 0.61 PLN/kWh
gross (2025-01-01 … 2026-12-31). The 2027 regulations cap "the cost of
electricity" at 0.50 PLN/kWh net (2027-01-01 … 2027-12-31) and no longer name
the handling fee. The first implementation had one limit, one validity window
and the cap price `(TGE + markup) × (1 + VAT)` hard-coded, so it could not
express 2027.

## Decision

- The configuration holds a list of editions (`tarcza_terms`), each with a
  name, validity, limit, whether the limit includes VAT and whether the
  handling fee counts in the average. An empty list means no cap.
- An edition spans whole calendar months, and editions may not overlap. The
  seller settles whole months and no edition says how a partly covered month
  would be billed, so the form refuses such dates instead of guessing.
- Unusable stored editions (malformed, partial months, overlaps) disable the
  cap as a whole and the reason reaches the entities and the log.
- Everything is computed in gross PLN, the basis the optimizer prices in. A
  net limit is grossed up with the VAT rate of the price settings; this
  assumes one VAT rate within a month.
- The billing module sets each slot's cap price under the edition of its
  month; the price module only supplies the components (TGE, markup, VAT).
- Hours of a month no edition covers keep their hourly energy price in the
  optimizer, and a capped running month needs its observed month-to-date
  position while a capped month that has not started does not.

## Rejected

- Keeping one limit and validity and editing them at each new edition: the
  months before the switch would be re-settled under the new terms.
- Partial-month editions settled pro rata: no regulation defines it.
- Automatic migration of the single-limit keys: they never shipped.
