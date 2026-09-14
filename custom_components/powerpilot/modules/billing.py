"""Monthly price-cap settlement ("Tarcza Pstryk") per point of delivery.

The seller caps the **monthly weighted-average** energy price: at month end
the energy bill of a point of delivery (PPE) is::

    K = min(C, limit × E)          C = Σ import[t] × cap_price[t]
                                   E = Σ import[t]

over every hour of the calendar month, where ``cap_price`` is the gross energy
price *without excise* (energy + handling fee — the components the promotion
covers; akcyza and distribution are billed outside it). This is a settlement
over the whole month, **not** an hourly cap — ``Σ import[t] × min(price[t],
limit)`` would be wrong.

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

from ..const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_TARCZA_ENABLED,
    CONF_TARCZA_LIMIT_KWH,
    CONF_TARCZA_VALID_FROM,
    CONF_TARCZA_VALID_TO,
    DEFAULTS,
    PPE_1,
)
from ..models import BillingPeriod, Forecast, ValidityRange
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

# How many gap descriptions a period carries (the first ones, plus a count).
_MAX_MISSING_LISTED = 4


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


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


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
        self._validity_warned = False

    # ------------------------------------------------------------ configuration

    @property
    def enabled(self) -> bool:
        return bool(self.config.get(CONF_TARCZA_ENABLED))

    @property
    def limit_kwh(self) -> float:
        return float(
            self.config.get(CONF_TARCZA_LIMIT_KWH, DEFAULTS[CONF_TARCZA_LIMIT_KWH])
        )

    def validity(self) -> ValidityRange | None:
        """The configured validity window; ``None`` when a date is malformed."""
        try:
            valid_from = _parse_date(
                self.config.get(CONF_TARCZA_VALID_FROM, DEFAULTS[CONF_TARCZA_VALID_FROM])
            )
            valid_to = _parse_date(
                self.config.get(CONF_TARCZA_VALID_TO, DEFAULTS[CONF_TARCZA_VALID_TO])
            )
        except ValueError:
            return None
        return ValidityRange(valid_from=valid_from, valid_to=valid_to)

    def active_on(self, day: date) -> bool:
        """Whether the cap applies on ``day`` (enabled and inside its validity)."""
        if not self.enabled:
            return False
        validity = self.validity()
        if validity is None:
            return False
        return validity.contains(day)

    def ppe_sensors(self) -> dict[str, list[str]]:
        """Configured grid-import meters per PPE (only PPE 1 today)."""
        sensors = [str(e) for e in (self.config.get(CONF_PPE1_IMPORT_SENSORS) or [])]
        return {PPE_1: sensors} if sensors else {}

    # ------------------------------------------------------------------ periods

    def empty_period(self, ppe: str, month_start: datetime) -> BillingPeriod:
        """A period with nothing observed yet (a month that has not started)."""
        _, month_end = month_bounds(month_start)
        return BillingPeriod(
            ppe=ppe,
            month_start=month_start,
            month_end=month_end,
            limit_kwh=self.limit_kwh,
        )

    def periods_touching(
        self, ppe: str, first: datetime, last: datetime
    ) -> list[BillingPeriod]:
        """Billing periods of ``ppe`` covering the hours ``[first, last]``.

        The current month comes from the observed position; later months start
        empty. Months the cap is not active in are skipped, so an empty result
        means "no cap applies to any of these hours".
        """
        periods: list[BillingPeriod] = []
        month_start, month_end = month_bounds(first)
        while month_start <= last:
            if self.active_on(dt_util.as_local(month_start).date()):
                observed = self.periods.get(ppe)
                if observed is not None and observed.month_start == month_start:
                    periods.append(observed)
                else:
                    periods.append(self.empty_period(ppe, month_start))
            month_start = month_end
            _, month_end = month_bounds(month_start)
        return periods

    # ------------------------------------------------------------------- update

    async def async_update(self) -> None:
        self.reasons = []
        if not self.enabled:
            self.periods = {}
            return
        validity = self.validity()
        if validity is None:
            self.periods = {}
            self.reasons.append("niepoprawne daty ważności Tarczy w konfiguracji")
            if not self._validity_warned:
                self.log_warning(
                    "Daty ważności Tarczy są niepoprawne (oczekiwany format "
                    "RRRR-MM-DD) — rozliczenie niedostępne.",
                    extra={
                        "valid_from": self.config.get(CONF_TARCZA_VALID_FROM),
                        "valid_to": self.config.get(CONF_TARCZA_VALID_TO),
                    },
                )
                self._validity_warned = True
            return
        self._validity_warned = False

        now = dt_util.now()
        if not validity.contains(now.date()):
            self.periods = {}
            self.reasons.append(
                f"Tarcza nieaktywna dziś (ważność {validity.valid_from} – "
                f"{validity.valid_to})"
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
                periods[ppe] = await self._observe(ppe, sensors, now)
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
        self, ppe: str, sensors: list[str], now: datetime
    ) -> BillingPeriod:
        """Read the month-to-date import + cap cost of one PPE."""
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
        gaps = 0

        def note(reason: str) -> None:
            nonlocal gaps
            gaps += 1
            if len(missing) < _MAX_MISSING_LISTED:
                missing.append(reason)

        hour = month_start
        while hour < hour_now:
            expected += 1
            label = dt_util.as_local(hour).strftime("%d.%m %H:%M")
            absent = [eid for eid in sensors if hour not in series[eid]]
            cap_price = self._prices.cap_price_at(hour)
            if absent:
                note(f"{label}: brak odczytu {', '.join(absent)}")
            elif cap_price is None:
                note(f"{label}: brak ceny pod Tarczę")
            else:
                kwh = sum(series[eid][hour] for eid in sensors)
                energy += kwh
                cost += kwh * cap_price
                covered += 1
            hour += timedelta(hours=1)

        # The running hour: its elapsed part from 5-minute statistics. Counted
        # as one more expected segment so a stale recorder shows up as a gap.
        if now > hour_now:
            expected += 1
            label = dt_util.as_local(hour_now).strftime("%d.%m %H:%M")
            partials = {
                eid: await self._meters.async_partial_kwh(eid, hour_now, now)
                for eid in sensors
            }
            absent = [eid for eid, value in partials.items() if value is None]
            cap_price = self._prices.cap_price_at(hour_now)
            if absent:
                note(f"{label} (bieżąca): brak odczytu {', '.join(absent)}")
            elif cap_price is None:
                note(f"{label} (bieżąca): brak ceny pod Tarczę")
            else:
                kwh = sum(partials.values())
                energy += kwh
                cost += kwh * cap_price
                covered += 1

        if gaps > len(missing):
            missing.append(f"… i {gaps - len(missing)} więcej")

        return BillingPeriod(
            ppe=ppe,
            month_start=month_start,
            month_end=month_end,
            limit_kwh=self.limit_kwh,
            energy_kwh=energy,
            cost_pln=cost,
            hours_expected=expected,
            hours_covered=covered,
            missing=tuple(missing),
        )

    # --------------------------------------------------------------- contribute

    def contribute(self, forecast: Forecast) -> None:
        """Nothing per slot — the cap is a monthly quantity, not an hourly one."""

    # -------------------------------------------------------------- diagnostics

    def status(self) -> dict:
        validity = self.validity()
        return {
            "enabled": self.enabled,
            "limit_kwh": self.limit_kwh,
            "valid_from": validity.valid_from.isoformat()
            if validity and validity.valid_from
            else None,
            "valid_to": validity.valid_to.isoformat()
            if validity and validity.valid_to
            else None,
            "active_today": self.active_on(dt_util.now().date()),
            "sensors": self.ppe_sensors(),
            "reasons": list(self.reasons),
            "periods": {ppe: p.as_dict() for ppe, p in self.periods.items()},
        }
