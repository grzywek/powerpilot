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
recorder and the price archive. PPE 1's import is the sum of its configured
grid-import meters. PPE 2 feeds only the apartment, so its import is the
apartment meter's energy in the intervals the apartment was on the second
meter (the supply module's per-hour states; hours containing a switch are
split at each transition's ``at`` by the 5-minute statistics). Both meters are
settled under the same edition of the terms. The optimizer adds the import it
plans (and the estimated tail of the month) on top and decides the battery
under the resulting regime (see ``optimizer.py``).

PPE 2's split hours are archived (``SplitArchive``, its own ``Store``): the
5-minute statistics a split is read from are purged after about ten days,
while the month needs every hour. The archive is not a second source — it
records the result of the one rule (the split) for completed hours whose
5-minute windows were all read past the recorder's latency, keyed by the
exact windows read, so it is authoritative only for the past and only while
the supply log still cuts the hour the same way. A mixed completed hour is
read from a matching archive entry, else split from the 5-minute statistics
(and archived), else it is a gap as any unreadable hour.

Data discipline: an hour missing an import reading from **any** of the PPE's
meters, or missing its cap price, or (PPE 2) whose supply the log cannot tell,
is an *unknown* hour — it is counted in ``hours_expected`` but not in
``hours_covered``, and the period is reported as incomplete with the reasons.
No zeros, no substitutes.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_PRICE_VAT,
    CONF_TARCZA_TERMS,
    DOMAIN,
    PPE_1,
    PPE_2,
    SUPPLY_HOUR_MIXED,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
    STORAGE_VERSION_BILLING_PPE2,
)
from ..models import (
    CAP_TERMS_DATES_ORDER,
    CAP_TERMS_LIMIT,
    CAP_TERMS_OVERLAP,
    CAP_TERMS_WHOLE_MONTHS,
    BillingPeriod,
    Forecast,
    PriceCapTerms,
    SupplyTransition,
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

_HOUR = timedelta(hours=1)
# The recorder's short-term statistics: 5-minute buckets on the UTC clock.
_BUCKET_MINUTES = 5

# Why an hour of PPE 2 cannot be known: the log cannot tell (part of) it.
_SUPPLY_UNKNOWN = "stan zasilania mieszkania nieznany"

# Every point of delivery the module settles.
_PPES = (PPE_1, PPE_2)

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


@dataclass(frozen=True)
class _HourRead:
    """What one hour of a PPE is made of (the running hour: its elapsed part).

    ``hourly``: meters read whole from the hourly statistics (completed hours);
    ``windows``: ``(meter, start, end)`` parts read from the 5-minute
    statistics; ``gap``: why the hour cannot be known at all. Nothing to read
    and no gap is a known 0 kWh.
    """

    hourly: tuple[str, ...] = ()
    windows: tuple[tuple[str, datetime, datetime], ...] = ()
    gap: str | None = None


# A 5-minute window an hour's energy was read from: ``(meter, start, end)``.
_Window = tuple[str, datetime, datetime]


def _parse_aware(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name}: expected an ISO timestamp, got {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{name}: timestamp without a zone: {value!r}")
    return moment.astimezone(timezone.utc)


class SplitArchive:
    """PPE 2's completed split hours: the kWh each was read as, and from which windows.

    One entry per hour (UTC start). An entry answers only for exactly the
    windows it was read from — meter and bounds — so a supply log rebuilt
    under another rule, or another apartment meter, never reuses a stale
    split; recording a fresh split replaces the hour's entry.
    """

    def __init__(self, entries: dict[datetime, tuple[tuple[_Window, ...], float]] | None = None) -> None:
        self._entries = dict(entries or {})
        self.dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(windows: Sequence[_Window]) -> tuple[_Window, ...]:
        return tuple(
            sorted((eid, start.astimezone(timezone.utc), end.astimezone(timezone.utc))
                   for eid, start, end in windows)
        )

    def lookup(self, hour: datetime, windows: Sequence[_Window]) -> float | None:
        entry = self._entries.get(hour.astimezone(timezone.utc))
        if entry is None or entry[0] != self._key(windows):
            return None
        return entry[1]

    def record(self, hour: datetime, windows: Sequence[_Window], kwh: float) -> None:
        key = hour.astimezone(timezone.utc)
        entry = (self._key(windows), kwh)
        if self._entries.get(key) != entry:
            self._entries[key] = entry
            self.dirty = True

    def prune(self, before: datetime) -> int:
        """Drop the hours starting before ``before``; returns how many."""
        old = [hour for hour in self._entries if hour < before]
        for hour in old:
            del self._entries[hour]
        if old:
            self.dirty = True
        return len(old)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hours": [
                {
                    "hour": hour.isoformat(),
                    "windows": [[eid, start.isoformat(), end.isoformat()] for eid, start, end in windows],
                    "kwh": kwh,
                }
                for hour, (windows, kwh) in sorted(self._entries.items())
            ]
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SplitArchive":
        """Decode a stored archive; ``ValueError`` when anything in it is unusable."""
        if not isinstance(data, dict) or not isinstance(data.get("hours"), list):
            raise ValueError("expected {'hours': [...]}")
        entries: dict[datetime, tuple[tuple[_Window, ...], float]] = {}
        for index, item in enumerate(data["hours"]):
            if not isinstance(item, dict):
                raise ValueError(f"hours[{index}]: expected an object")
            hour = _parse_aware(item.get("hour"), f"hours[{index}].hour")
            raw_windows = item.get("windows")
            if not isinstance(raw_windows, list) or not raw_windows:
                raise ValueError(f"hours[{index}].windows: expected a non-empty list")
            windows: list[_Window] = []
            for w_index, window in enumerate(raw_windows):
                name = f"hours[{index}].windows[{w_index}]"
                if not isinstance(window, list) or len(window) != 3 or not isinstance(window[0], str):
                    raise ValueError(f"{name}: expected [meter, start, end]")
                start = _parse_aware(window[1], name)
                end = _parse_aware(window[2], name)
                if not hour <= start < end <= hour + _HOUR:
                    raise ValueError(f"{name}: window outside its hour")
                windows.append((window[0], start, end))
            kwh = item.get("kwh")
            if isinstance(kwh, bool) or not isinstance(kwh, (int, float)) or not math.isfinite(kwh):
                raise ValueError(f"hours[{index}].kwh: expected a number, got {kwh!r}")
            if hour in entries:
                raise ValueError(f"hours[{index}]: hour {hour.isoformat()} listed twice")
            entries[hour] = (cls._key(windows), float(kwh))
        return cls(entries)


class _PpeHours(Protocol):
    """How a PPE's import in an hour is read; the settlement rules are shared."""

    meters: tuple[str, ...]  # meters whose hourly statistics the month needs

    def completed(self, hour: datetime) -> _HourRead: ...

    def running(self, hour: datetime, now: datetime) -> _HourRead: ...


class _MeterSumHours:
    """PPE 1: the sum of its grid-import meters, every hour."""

    def __init__(self, sensors: Sequence[str]) -> None:
        self.meters = tuple(sensors)

    def completed(self, hour: datetime) -> _HourRead:
        return _HourRead(hourly=self.meters)

    def running(self, hour: datetime, now: datetime) -> _HourRead:
        return _HourRead(windows=tuple((eid, hour, now) for eid in self.meters))


def _bucket_start(moment: datetime) -> datetime:
    """Start of the 5-minute statistics bucket containing ``moment`` (UTC)."""
    moment = moment.astimezone(timezone.utc)
    return moment.replace(
        minute=moment.minute - moment.minute % _BUCKET_MINUTES, second=0, microsecond=0
    )


def supply_segments(
    transitions: Sequence[SupplyTransition], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, str | None]]:
    """``[start, end)`` cut at each transition's ``at`` into ``(from, to, state)``.

    Each switch is cut at the start of the 5-minute bucket its ``at`` falls in
    (``at`` is the first sample of the new state, so the switch happened at or
    before it; the 5-minute statistics cannot be cut finer). The part before
    the first switch inside has the state of the last transition before
    ``start`` — ``None`` when the log has none. Adjacent parts of one state
    are merged; switches sharing a bucket leave the later state.
    """
    state: str | None = None
    for transition in transitions:
        if transition.at >= start:
            break
        state = transition.state
    segments: list[tuple[datetime, datetime, str | None]] = []
    cursor = start
    for transition in transitions:
        if not start <= transition.at < end:
            continue
        cut = max(_bucket_start(transition.at), start)
        if cut > cursor:
            segments.append((cursor, cut, state))
            cursor = cut
        state = transition.state
    if end > cursor:
        segments.append((cursor, end, state))
    merged: list[tuple[datetime, datetime, str | None]] = []
    for segment in segments:
        if merged and merged[-1][2] == segment[2]:
            merged[-1] = (merged[-1][0], segment[1], segment[2])
        else:
            merged.append(segment)
    return merged


class _ApartmentOnMeter2Hours:
    """PPE 2: the apartment meter's energy while the apartment was on meter 2.

    Completed hours by their supply (``SupplyModule.async_past_hour_states``):
    ``victron`` is 0 kWh, ``meter_2`` the whole hour's apartment energy,
    ``mixed`` the meter-2 parts between the switches inside it, ``unknown`` a
    gap. The running hour follows the confirmed state — the last transition —
    split the same way at any switch inside it.
    """

    def __init__(
        self,
        apartment: str,
        states: Mapping[datetime, str],
        transitions: Sequence[SupplyTransition],
    ) -> None:
        self.meters = (apartment,)
        self._apartment = apartment
        self._states = states
        self._transitions = transitions

    def completed(self, hour: datetime) -> _HourRead:
        supply = self._states[hour.astimezone(timezone.utc)]
        if supply == SUPPLY_VICTRON:
            return _HourRead()
        if supply == SUPPLY_METER_2:
            return _HourRead(hourly=self.meters)
        if supply == SUPPLY_HOUR_MIXED:
            return self._split(hour, hour + _HOUR, whole_hour=True)
        return _HourRead(gap=_SUPPLY_UNKNOWN)

    def running(self, hour: datetime, now: datetime) -> _HourRead:
        return self._split(hour, now, whole_hour=False)

    def _split(self, start: datetime, end: datetime, *, whole_hour: bool) -> _HourRead:
        segments = supply_segments(self._transitions, start, end)
        if any(state is None for _, _, state in segments):
            return _HourRead(gap=_SUPPLY_UNKNOWN)
        on_meter_2 = [(a, b) for a, b, state in segments if state == SUPPLY_METER_2]
        if whole_hour and on_meter_2 == [(start, end)]:
            # No switch inside after all: the hour's own statistic.
            return _HourRead(hourly=self.meters)
        return _HourRead(windows=tuple((self._apartment, a, b) for a, b in on_meter_2))


class BillingModule(PowerPilotModule):
    """Observed month-to-date settlement position of every configured PPE."""

    domain = "billing"

    def __init__(self, hass, coordinator, meters, prices, supply) -> None:
        super().__init__(hass, coordinator)
        # Recorder-backed kWh readers (``async_range_kwh`` / ``async_partial_kwh``),
        # the cap-price resolver and the apartment's supply (which hours PPE 2
        # fed it) — injected explicitly rather than reached through the
        # coordinator, so the dependency is visible and testable.
        self._meters = meters
        self._prices = prices
        self._supply = supply
        # Current-month observed periods keyed by PPE id.
        self.periods: dict[str, BillingPeriod] = {}
        # Why a PPE has no (or a failed) period this cycle, keyed by PPE id;
        # module-wide reasons are recorded on every PPE.
        self._reasons: dict[str, list[str]] = {}
        self._config_warned: str | None = None
        # PPE 2's split hours, persisted once ``async_setup`` has loaded them;
        # ``None`` (with ``archive_error``) while the stored archive is unusable.
        self.archive: SplitArchive | None = SplitArchive()
        self.archive_error: str | None = None
        self.archive_save_error: str | None = None
        self._archive_store: Store | None = None

    # -------------------------------------------------------------------- setup

    async def async_setup(self) -> None:
        self._archive_store = Store(
            self.hass,
            STORAGE_VERSION_BILLING_PPE2,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_billing_ppe2",
            atomic_writes=True,
        )
        stored = await self._archive_store.async_load()
        if stored is None:
            return
        try:
            self.archive = SplitArchive.from_dict(stored)
        except ValueError as err:
            await self._async_move_archive_aside(err)

    async def _async_move_archive_aside(self, err: ValueError) -> None:
        """Keep an undecodable archive for inspection and never save over it this run.

        PPE 2 is then read without the archive: a split hour whose 5-minute
        statistics are gone is a gap, and PPE 2's reasons say why.
        """
        assert self._archive_store is not None
        self.archive = None
        path = self._archive_store.path
        aside = f"{path}.corrupt-{dt_util.utcnow():%Y-%m-%dT%H%M%S}"
        try:
            await self.hass.async_add_executor_job(os.rename, path, aside)
        except OSError as move_err:
            _LOGGER.error(
                "PPE 2 split archive %s cannot be decoded (%s) nor moved aside (%s); "
                "it is left as is and not written this run",
                path,
                err,
                move_err,
            )
            self.archive_error = (
                f"archiwum podzielonych godzin PPE 2 jest nieczytelne ({err}) i nie dało "
                f"się go odłożyć ({move_err}) — godziny z przełączeniem starsze niż "
                f"statystyki 5-minutowe są lukami; popraw lub usuń {path} i uruchom ponownie"
            )
        else:
            _LOGGER.warning(
                "PPE 2 split archive %s cannot be decoded (%s); moved to %s as corrupt. "
                "PPE 2 is read without it until the next restart",
                path,
                err,
                aside,
            )
            self.archive_error = (
                f"archiwum podzielonych godzin PPE 2 było nieczytelne ({err}) i zostało "
                f"odłożone do {aside} — do restartu godziny z przełączeniem starsze niż "
                f"statystyki 5-minutowe są lukami; po nim archiwum zacznie się od nowa"
            )
        self.log_warning(self.archive_error)

    async def _async_save_archive(self, now: datetime) -> None:
        """Prune hours before the previous month and save, when anything changed."""
        archive = self.archive
        if archive is None or self._archive_store is None:
            return
        month_start, _ = month_bounds(now)
        previous = dt_util.as_local(month_start).date() - timedelta(days=1)
        archive.prune(dt_util.start_of_local_day(previous.replace(day=1)))
        if not archive.dirty:
            return
        try:
            await self._archive_store.async_save(archive.to_dict())
        except Exception as err:  # any store failure; surfaced below
            _LOGGER.exception("Saving the PPE 2 split archive failed")
            self.archive_save_error = (
                f"nie udało się zapisać archiwum podzielonych godzin PPE 2 ({err}); "
                "zapis zostanie ponowiony przy następnym odświeżeniu"
            )
            self.log_warning(self.archive_save_error)
        else:
            archive.dirty = False
            self.archive_save_error = None

    async def async_clear_data(self) -> None:
        if self._archive_store is not None:
            await self._archive_store.async_remove()
        self.archive = SplitArchive()
        self.archive_error = None
        self.archive_save_error = None

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
        """Meters each PPE is read from: PPE 1's grid-import meters, PPE 2's apartment meter.

        PPE 2 is listed only while the supply detection shapes the meter tree
        (its import is the apartment meter's energy in meter-2 intervals).
        """
        out: dict[str, list[str]] = {}
        sensors = [str(e) for e in (self.config.get(CONF_PPE1_IMPORT_SENSORS) or [])]
        if sensors:
            out[PPE_1] = sensors
        if self._supply.active:
            out[PPE_2] = [self._supply.apartment_meter]
        return out

    def reasons_for(self, ppe: str) -> list[str]:
        """Why ``ppe`` has no period this cycle, or why its read failed (empty when fine)."""
        return list(self._reasons.get(ppe, []))

    @property
    def reasons(self) -> list[str]:
        """PPE 1's reasons — the plan settles PPE 1 only so far."""
        return self.reasons_for(PPE_1)

    def _reason(self, ppe: str, reason: str) -> None:
        self._reasons.setdefault(ppe, []).append(reason)

    def _reason_all(self, reason: str) -> None:
        for ppe in _PPES:
            self._reason(ppe, reason)

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
        self._reasons = {}
        if not self.enabled:
            self.periods = {}
            return
        error = self.config_error
        if error is not None:
            self.periods = {}
            self._reason_all(error)
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
            self._reason_all(
                f"Tarcza nieaktywna dziś — żadna skonfigurowana Tarcza nie "
                f"obejmuje {dt_util.as_local(now).date().isoformat()}"
            )
            return

        # Never keep a stale period across a failed read: the reason is
        # recorded on that PPE and the error propagates to the module
        # registry's log. One PPE's failure leaves the other's period standing.
        self.periods = {}
        periods: dict[str, BillingPeriod] = {}
        errors: list[Exception] = []
        for ppe, observe in ((PPE_1, self._observe_ppe1), (PPE_2, self._observe_ppe2)):
            try:
                period = await observe(now, terms)
            except Exception as err:
                self._reason(ppe, f"błąd odczytu liczników {ppe}: {err}")
                if errors:
                    _LOGGER.error("Reading the observed month of %s failed", ppe, exc_info=err)
                errors.append(err)
                continue
            if period is not None:
                periods[ppe] = period
        self.periods = periods
        await self._async_save_archive(now)

        for period in periods.values():
            self.log_info(
                f"Tarcza {period.ppe}: {period.energy_kwh:.1f} kWh za "
                f"{period.cost_pln:.2f} zł od początku miesiąca "
                f"({period.hours_covered}/{period.hours_expected} h"
                f"{'' if period.complete else ', NIEKOMPLETNE'}).",
                extra=period.as_dict(),
            )
        if errors:
            raise errors[0]

    async def _observe_ppe1(
        self, now: datetime, terms: PriceCapTerms
    ) -> BillingPeriod | None:
        """PPE 1: the sum of its grid-import meters in every hour."""
        sensors = self.ppe_sensors().get(PPE_1)
        if not sensors:
            self._reason(PPE_1, "brak skonfigurowanych liczników importu PPE 1")
            return None
        return await self._observe(PPE_1, _MeterSumHours(sensors), now, terms)

    async def _observe_ppe2(
        self, now: datetime, terms: PriceCapTerms
    ) -> BillingPeriod | None:
        """PPE 2: the apartment meter's energy while the apartment was on meter 2.

        Settled under the same edition as PPE 1 (both meters have the same
        terms). The supply of every completed hour comes from the supply
        module's one function for past hours; the transition log only places
        the switches inside the hours it calls ``mixed`` and the running one.
        """
        supply = self._supply
        if not supply.active:
            self._reason(
                PPE_2,
                "wykrywanie zasilania mieszkania wyłączone — ustaw czujniki i "
                "licznik mieszkania w opcjach „Zasilanie mieszkania”",
            )
            return None
        if not supply.history_ready:
            # Hours read now would be told from hourly means where the log,
            # still being replayed, has its own answer.
            self._reason(PPE_2, "historia zasilania mieszkania jeszcze się odtwarza")
            return None
        month_start, _ = month_bounds(now)
        hour_now = now.replace(minute=0, second=0, microsecond=0)
        transitions = list(supply.transitions)
        states = await supply.async_past_hour_states(month_start, hour_now)
        hours = _ApartmentOnMeter2Hours(supply.apartment_meter, states, transitions)
        for problem in (self.archive_error, self.archive_save_error):
            if problem is not None:
                self._reason(PPE_2, problem)
        return await self._observe(PPE_2, hours, now, terms, archive=self.archive)

    async def _observe(
        self,
        ppe: str,
        hours: "_PpeHours",
        now: datetime,
        terms: PriceCapTerms,
        archive: SplitArchive | None = None,
    ) -> BillingPeriod:
        """Read the month-to-date import + cap cost of one PPE under ``terms``.

        ``hours`` says what each hour of the PPE is made of; the rules for
        expected, covered, missing and pending hours, the cap price and the
        recorder's latency are the same for every PPE and live here.
        ``archive`` keeps the 5-minute reads of completed hours (PPE 2's
        splits) past the short-term statistics' retention.
        """
        month_start, month_end = month_bounds(now)
        hour_now = now.replace(minute=0, second=0, microsecond=0)

        # Completed hours: hourly statistics. The reader drops the first row of
        # a window (it needs a predecessor to difference), hence the lead-in.
        series: dict[str, dict[datetime, float]] = {}
        for entity_id in hours.meters:
            series[entity_id] = await self._meters.async_range_kwh(
                entity_id, month_start - _HOUR, hour_now
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

        async def account(
            label: str, hour: datetime, read: _HourRead, completed: bool
        ) -> None:
            nonlocal energy, cost, covered
            if read.gap is not None:
                note(f"{label}: {read.gap}")
                return
            values: list[float] = []
            absent: list[str] = []
            from_buckets = False
            for eid in read.hourly:
                value = series[eid].get(hour)
                if value is None and now - (hour + _HOUR) <= _RECORDER_LATENCY:
                    # The hourly statistic of an hour that just ended is still
                    # on its way: read the hour from its 5-minute buckets. An
                    # hour without a single bucket is a real hole in the
                    # recorder.
                    value = await self._meters.async_partial_kwh(eid, hour, hour + _HOUR)
                    from_buckets = from_buckets or value is not None
                if value is None:
                    absent.append(eid)
                else:
                    values.append(value)
            waiting: list[datetime] = []
            archivable = (
                archive is not None
                and completed
                and bool(read.windows)
                and now - (hour + _HOUR) > _RECORDER_LATENCY
            )
            archived = archive.lookup(hour, read.windows) if archivable and archive else None
            if archived is not None:
                values.append(archived)
            else:
                window_values: list[float] = []
                window_absent = False
                for eid, start, end in read.windows:
                    value = await self._meters.async_partial_kwh(eid, start, end)
                    if value is not None:
                        window_values.append(value)
                    elif now - start <= _RECORDER_LATENCY:
                        # Nothing of this window compiled yet: its minutes are
                        # pending, counted by the next refresh that sees them.
                        waiting.append(start)
                    else:
                        absent.append(eid)
                        window_absent = True
                values.extend(window_values)
                if archivable and archive is not None and not waiting and not window_absent:
                    # Every window read, past the latency: the split is final.
                    archive.record(hour, read.windows, sum(window_values))
            if from_buckets and not any(eid in absent for eid in read.hourly):
                pending.append(f"{label}: statystyka godzinowa w drodze — liczona z 5-minutowych")
            if waiting and not absent:
                first = min(waiting)
                if first == hour:
                    pending.append(f"{label}: pierwsze minuty w drodze")
                else:
                    when = dt_util.as_local(first).strftime("%H:%M")
                    pending.append(f"{label}: minuty od {when} w drodze")
                covered += 1
                return
            cap_price = self._prices.cap_price_at(hour, terms)
            if absent:
                note(f"{label}: brak odczytu {', '.join(dict.fromkeys(absent))}")
            elif cap_price is None:
                note(f"{label}: brak ceny pod Tarczę")
            else:
                kwh = sum(values)
                energy += kwh
                cost += kwh * cap_price
                covered += 1

        hour = month_start
        while hour < hour_now:
            expected += 1
            label = dt_util.as_local(hour).strftime("%d.%m %H:%M")
            await account(label, hour, hours.completed(hour), True)
            hour += _HOUR

        # The running hour: its elapsed part from 5-minute statistics. Counted
        # as one more expected segment so a stale recorder shows up as a gap —
        # once its first bucket is overdue, not while it is being compiled.
        if now > hour_now:
            expected += 1
            label = dt_util.as_local(hour_now).strftime("%d.%m %H:%M")
            await account(f"{label} (bieżąca)", hour_now, hours.running(hour_now, now), False)

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
            "reasons": {ppe: self.reasons_for(ppe) for ppe in _PPES},
            "periods": {ppe: p.as_dict() for ppe, p in self.periods.items()},
            "ppe2_archive": {
                "hours": len(self.archive) if self.archive is not None else None,
                "error": self.archive_error,
                "save_error": self.archive_save_error,
            },
        }
