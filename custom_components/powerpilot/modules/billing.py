"""Monthly price-cap settlement ("Tarcza Pstryk") per point of delivery.

The seller caps the **monthly weighted-average** energy price: at month end
the energy bill of a point of delivery (PPE) is::

    K = min(C, limit × E)          C = Σ import[t] × cap_price[t]
                                   E = Σ import[t]

over every hour of the calendar month, where ``cap_price`` is the gross price
of the hour's kWh as the edition of the terms in force that month averages it
(``PriceCapTerms.cap_price``: energy, plus the handling fee when the edition
counts it; excise and distribution never). This is a settlement over the
whole month, **not** an hourly cap — ``Σ import[t] × min(price[t], limit)``
would be wrong.

Each edition of the terms (``CONF_TARCZA_TERMS``) spans whole months with its
own limit and basis; a month no edition covers has no cap at all.

This module owns the *observed* part of that settlement: the import of each
PPE since the month started and its cost at the cap price, read from the
configured grid-import meters and the price archive. The optimizer adds the
import it plans (and the estimated tail of the month) on top and decides the
battery under the resulting regime (see ``optimizer.py``).

Data discipline: an hour missing an import reading from **any** of the PPE's
meters, or missing its cap price, is an *unknown* hour — it is counted in
``hours_expected`` but not in ``hours_covered``, and the period is reported as
incomplete with the reasons. No zeros, no substitutes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.util import dt as dt_util

from ..const import CONF_PPE1_IMPORT_SENSORS, CONF_PRICE_VAT, CONF_TARCZA_TERMS, PPE_1
from ..models import (
    CAP_TERMS_DATES_ORDER,
    CAP_TERMS_LIMIT,
    CAP_TERMS_OVERLAP,
    CAP_TERMS_WHOLE_MONTHS,
    BillingPeriod,
    Forecast,
    PriceCapTerms,
    price_cap_problems,
    price_cap_terms_for_day,
)
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

# How many gap descriptions a period carries (the first ones, plus a count).
_MAX_MISSING_LISTED = 4

# How long a statistic may still be on its way before its absence is a gap.
# The recorder compiles each 5-minute bucket, and at the top of the hour the
# hour that ended, about ten seconds after they close; the plan re-runs at the
# hour boundary itself. 15 minutes lets two compile cycles slip before an
# absent statistic counts as missing.
_RECORDER_LATENCY = timedelta(minutes=15)

# What each unusable-terms code means, for the reasons on the entities.
_PROBLEM_TEXT = {
    CAP_TERMS_DATES_ORDER: "data początku po dacie końca",
    CAP_TERMS_WHOLE_MONTHS: "okres nie obejmuje pełnych miesięcy",
    CAP_TERMS_LIMIT: "limit musi być większy od zera",
    CAP_TERMS_OVERLAP: "okresy Tarcz nakładają się",
}


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """``(first hour of the month, first hour of the next month)`` in local time."""
    local = dt_util.as_local(moment)
    start = dt_util.start_of_local_day(local.date().replace(day=1))
    end = dt_util.start_of_local_day(next_month_start(local.date()))
    return start, end


def next_month_start(day: date) -> date:
    """First day of the month after the one containing ``day``."""
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


class BillingModule(PowerPilotModule):
    """Observed month-to-date settlement position of every configured PPE."""

    domain = "billing"

    def __init__(self, hass, coordinator, meters, prices) -> None:
        super().__init__(hass, coordinator)
        # Recorder-backed kWh readers (``async_range_kwh`` / ``async_partial_kwh``)
        # and the cap-price resolver — injected explicitly rather than reached
        # through the coordinator, so the dependency is visible and testable.
        self._meters = meters
        self._prices = prices
        # Current-month observed periods keyed by PPE id.
        self.periods: dict[str, BillingPeriod] = {}
        # Why no period could be built this cycle (empty when fine).
        self.reasons: list[str] = []
        self._config_warned: str | None = None

    # ------------------------------------------------------------ configuration

    @property
    def enabled(self) -> bool:
        """Whether any edition of the terms is configured at all."""
        return bool(self.config.get(CONF_TARCZA_TERMS))

    def terms(self) -> list[PriceCapTerms]:
        """The configured editions; empty while they are unusable (see below)."""
        terms, error = self._load_terms()
        return [] if error else terms

    @property
    def config_error(self) -> str | None:
        """Why the configured editions cannot be used, ``None`` when they can.

        Unusable terms disable the cap as a whole — a month is never settled
        under a guessed edition — and the reason travels to the plan.
        """
        return self._load_terms()[1]

    def _load_terms(self) -> tuple[list[PriceCapTerms], str | None]:
        raw = self.config.get(CONF_TARCZA_TERMS) or []
        try:
            terms = [PriceCapTerms.from_dict(item) for item in raw]
        except (TypeError, ValueError, AttributeError) as err:
            return [], f"niepoprawna konfiguracja Tarczy: {err}"
        problems = price_cap_problems(terms)
        if problems:
            return [], "niepoprawna konfiguracja Tarczy: " + ", ".join(
                _PROBLEM_TEXT.get(code, code) for code in problems
            )
        return terms, None

    def terms_on(self, day: date) -> PriceCapTerms | None:
        """The edition in force on ``day`` (``None``: no cap that day)."""
        return price_cap_terms_for_day(self.terms(), day)

    def active_on(self, day: date) -> bool:
        """Whether the cap applies on ``day``."""
        return self.terms_on(day) is not None

    def vat_rate(self) -> float:
        """VAT rate the pricing config applies — grosses up a net limit."""
        return float(self.config.get(CONF_PRICE_VAT, 1.0) or 1.0) - 1.0

    def ppe_sensors(self) -> dict[str, list[str]]:
        """Configured grid-import meters per PPE (only PPE 1 today)."""
        sensors = [str(e) for e in (self.config.get(CONF_PPE1_IMPORT_SENSORS) or [])]
        return {PPE_1: sensors} if sensors else {}

    # ------------------------------------------------------------------ periods

    def empty_period(
        self, ppe: str, month_start: datetime, terms: PriceCapTerms
    ) -> BillingPeriod:
        """A period with nothing observed yet (a month that has not started)."""
        _, month_end = month_bounds(month_start)
        return BillingPeriod(
            ppe=ppe,
            month_start=month_start,
            month_end=month_end,
            limit_kwh=terms.gross_limit(self.vat_rate()),
            terms_name=terms.name,
        )

    def periods_touching(
        self, ppe: str, first: datetime, last: datetime
    ) -> list[BillingPeriod]:
        """Billing periods of ``ppe`` covering the hours ``[first, last]``.

        The current month comes from the observed position; later months start
        empty, each under its own edition of the terms. Months no edition
        covers are skipped, so an empty result means "no cap applies to any of
        these hours". The caller must not plan a capped current month without
        its observed position (``periods``) — this method cannot tell.
        """
        periods: list[BillingPeriod] = []
        month_start, month_end = month_bounds(first)
        while month_start <= last:
            terms = self.terms_on(dt_util.as_local(month_start).date())
            if terms is not None:
                observed = self.periods.get(ppe)
                if observed is not None and observed.month_start == month_start:
                    periods.append(observed)
                else:
                    periods.append(self.empty_period(ppe, month_start, terms))
            month_start = month_end
            _, month_end = month_bounds(month_start)
        return periods

    # ------------------------------------------------------------------- update

    async def async_update(self) -> None:
        self.reasons = []
        if not self.enabled:
            self.periods = {}
            return
        error = self.config_error
        if error is not None:
            self.periods = {}
            self.reasons.append(error)
            if error != self._config_warned:
                self.log_warning(
                    f"{error} — rozliczenie Tarczy niedostępne.",
                    extra={"terms": self.config.get(CONF_TARCZA_TERMS)},
                )
                self._config_warned = error
            return
        self._config_warned = None

        now = dt_util.now()
        terms = self.terms_on(dt_util.as_local(now).date())
        if terms is None:
            self.periods = {}
            self.reasons.append(
                f"Tarcza nieaktywna dziś — żadna skonfigurowana Tarcza nie "
                f"obejmuje {dt_util.as_local(now).date().isoformat()}"
            )
            return

        sensors_by_ppe = self.ppe_sensors()
        if not sensors_by_ppe:
            self.periods = {}
            self.reasons.append("brak skonfigurowanych liczników importu PPE 1")
            return

        # Never keep a stale period across a failed read: the reason is
        # recorded and the error propagates to the module registry's log.
        self.periods = {}
        periods: dict[str, BillingPeriod] = {}
        for ppe, sensors in sensors_by_ppe.items():
            try:
                periods[ppe] = await self._observe(ppe, sensors, now, terms)
            except Exception as err:
                self.reasons.append(f"błąd odczytu liczników {ppe}: {err}")
                raise
        self.periods = periods

        for period in periods.values():
            self.log_info(
                f"Tarcza {period.ppe}: {period.energy_kwh:.1f} kWh za "
                f"{period.cost_pln:.2f} zł od początku miesiąca "
                f"({period.hours_covered}/{period.hours_expected} h"
                f"{'' if period.complete else ', NIEKOMPLETNE'}).",
                extra=period.as_dict(),
            )

    async def _observe(
        self, ppe: str, sensors: list[str], now: datetime, terms: PriceCapTerms
    ) -> BillingPeriod:
        """Read the month-to-date import + cap cost of one PPE under ``terms``."""
        month_start, month_end = month_bounds(now)
        hour_now = now.replace(minute=0, second=0, microsecond=0)

        # Completed hours: hourly statistics. The reader drops the first row of
        # a window (it needs a predecessor to difference), hence the lead-in.
        series: dict[str, dict[datetime, float]] = {}
        for entity_id in sensors:
            series[entity_id] = await self._meters.async_range_kwh(
                entity_id, month_start - timedelta(hours=1), hour_now
            )

        energy = 0.0
        cost = 0.0
        expected = 0
        covered = 0
        missing: list[str] = []
        pending: list[str] = []
        gaps = 0

        def note(reason: str) -> None:
            nonlocal gaps
            gaps += 1
            if len(missing) < _MAX_MISSING_LISTED:
                missing.append(reason)

        def settle(label: str, hour: datetime, readings: dict[str, float | None]) -> None:
            nonlocal energy, cost, covered
            absent = [eid for eid, value in readings.items() if value is None]
            cap_price = self._prices.cap_price_at(hour, terms)
            if absent:
                note(f"{label}: brak odczytu {', '.join(absent)}")
            elif cap_price is None:
                note(f"{label}: brak ceny pod Tarczę")
            else:
                kwh = sum(readings.values())
                energy += kwh
                cost += kwh * cap_price
                covered += 1

        hour = month_start
        while hour < hour_now:
            expected += 1
            label = dt_util.as_local(hour).strftime("%d.%m %H:%M")
            readings: dict[str, float | None] = {
                eid: series[eid].get(hour) for eid in sensors
            }
            uncompiled = [eid for eid, value in readings.items() if value is None]
            if uncompiled and now - (hour + timedelta(hours=1)) <= _RECORDER_LATENCY:
                # The hourly statistic of an hour that just ended is still on
                # its way: read the hour from its 5-minute buckets. A bucket
                # not compiled yet is pending; an hour without a single
                # bucket is a real hole in the recorder.
                for eid in uncompiled:
                    readings[eid] = await self._meters.async_partial_kwh(
                        eid, hour, hour + timedelta(hours=1)
                    )
                if all(readings[eid] is not None for eid in uncompiled):
                    pending.append(
                        f"{label}: statystyka godzinowa w drodze — "
                        "liczona z 5-minutowych"
                    )
            settle(label, hour, readings)
            hour += timedelta(hours=1)

        # The running hour: its elapsed part from 5-minute statistics. Counted
        # as one more expected segment so a stale recorder shows up as a gap —
        # once its first bucket is overdue, not while it is being compiled.
        if now > hour_now:
            expected += 1
            label = dt_util.as_local(hour_now).strftime("%d.%m %H:%M")
            partials: dict[str, float | None] = {
                eid: await self._meters.async_partial_kwh(eid, hour_now, now)
                for eid in sensors
            }
            uncompiled = [eid for eid, value in partials.items() if value is None]
            if uncompiled and now - hour_now <= _RECORDER_LATENCY:
                # Nothing of this hour compiled yet: its minutes so far are
                # pending, counted by the next refresh that sees them.
                pending.append(f"{label} (bieżąca): pierwsze minuty w drodze")
                covered += 1
            else:
                settle(f"{label} (bieżąca)", hour_now, partials)

        if gaps > len(missing):
            missing.append(f"… i {gaps - len(missing)} więcej")

        return BillingPeriod(
            ppe=ppe,
            month_start=month_start,
            month_end=month_end,
            limit_kwh=terms.gross_limit(self.vat_rate()),
            terms_name=terms.name,
            energy_kwh=energy,
            cost_pln=cost,
            hours_expected=expected,
            hours_covered=covered,
            missing=tuple(missing),
            pending=tuple(pending),
        )

    # --------------------------------------------------------------- contribute

    def contribute(self, forecast: Forecast) -> None:
        """Every slot's cap price under the edition in force that month.

        Hours no edition covers keep ``cap_price_kwh = None``: their energy is
        billed at the hourly price, outside any monthly settlement.
        """
        terms = self.terms()
        if not terms:
            return
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            edition = price_cap_terms_for_day(terms, dt_util.as_local(hour).date())
            if edition is not None:
                slot.cap_price_kwh = self._prices.cap_price_for(hour, edition)

    # -------------------------------------------------------------- diagnostics

    def status(self) -> dict:
        today = self.terms_on(dt_util.as_local(dt_util.now()).date())
        return {
            "enabled": self.enabled,
            "config_error": self.config_error,
            "terms": [t.to_dict() for t in self.terms()],
            "active_today": today.name if today is not None else None,
            "sensors": self.ppe_sensors(),
            "reasons": list(self.reasons),
            "periods": {ppe: p.as_dict() for ppe, p in self.periods.items()},
        }
