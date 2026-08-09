"""WebSocket API tests — proves the panel's data endpoints work."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    INTEGRATION_VERSION,
)


async def _setup(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc"},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def _empty_recent_soc(*args):
        return {}

    coordinator._recent_soc = _empty_recent_soc


async def test_ws_plan_status_log(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/status"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "checks" in msg["result"]
    assert "modules" in msg["result"]
    assert msg["result"]["version"] == INTEGRATION_VERSION

    await client.send_json({"id": 2, "type": "powerpilot/plan"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "hours" in msg["result"]
    assert "forecast" in msg["result"]

    await client.send_json({"id": 3, "type": "powerpilot/log"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "events" in msg["result"]
    # The first optimization run should have recorded an event.
    assert len(msg["result"]["events"]) >= 1

    await client.send_json({"id": 4, "type": "powerpilot/profiles"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "consumption" in msg["result"]

    await client.send_json({"id": 6, "type": "powerpilot/series", "past_hours": 12})
    msg = await client.receive_json()
    assert msg["success"]
    assert "hours" in msg["result"]
    assert "now" in msg["result"]
    # Should contain both past hours and the forecast horizon.
    assert any(h["is_past"] for h in msg["result"]["hours"])

    # A forecast-lead selection is accepted (past "prognoza" comparison shift).
    await client.send_json(
        {"id": 7, "type": "powerpilot/series", "past_hours": 12, "forecast_lead": 6}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert "hours" in msg["result"]


async def test_series_forecast_lead_shifts_future(hass: HomeAssistant) -> None:
    """A forecast-lead selection pins the FUTURE prognoza to the older vintage.

    Regression: the lead selector only re-read past hours; future hours always
    showed the live plan. Now the future "prognoza" side comes from the single
    plan made ~lead hours ago, so the dashed line responds to the selector too.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    await _setup(hass)
    coordinator = hass.data[DOMAIN][
        next(iter(hass.data[DOMAIN]))
    ]

    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    old_start = now - timedelta(hours=6)
    # An older vintage whose SoC trajectory is a distinct sentinel, so a future
    # hour drawn from it is unmistakably not the live plan's own SoC.
    coordinator.snapshots.add(
        {
            "run_at": old_start.isoformat(),
            "start": old_start.isoformat(),
            "n": 12,
            "horizon_hours": 12,
            "total_cost": 1.0,
            "soc": [42.5] * 12,
            "grid": [0.0] * 12,
            "dischg": [0.0] * 12,
            "ev": [0.0] * 12,
            "charge": [0.0] * 12,
        }
    )

    def _future(result: dict) -> list[dict]:
        return [
            h for h in result["hours"] if not h["is_past"] and h.get("forecast")
        ]

    live = await coordinator.get_series(past_hours=1, forecast_lead=0)
    fut_live = _future(live)
    assert fut_live, "expected a future forecast horizon"
    assert fut_live[0]["forecast"]["soc_end"] != 42.5  # the live plan, not the vintage

    shifted = await coordinator.get_series(past_hours=1, forecast_lead=6)
    fut_shift = _future(shifted)
    assert fut_shift, "expected a future forecast horizon at lead 6"
    assert fut_shift[0]["forecast"]["soc_end"] == 42.5  # pinned to the 6-h-old vintage
    assert fut_shift[0]["forecast_origin"] == coordinator.snapshots._key(old_start)


async def test_series_lead_uses_single_pinned_vintage(hass: HomeAssistant) -> None:
    """At a stale lead, past prognoza SoC comes from ONE plan's trajectory.

    Regression: sliding a different (re-seeded) vintage under each past hour
    stitched them into a nonsensical SoC line — phantom rises in hours with no
    planned charging. The lead now pins the whole prognoza to the single plan
    made ~lead hours ago, so its SoC is one coherent trajectory.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    pin_start = now - timedelta(hours=6)
    soc_traj = [float(10 * (i + 1)) for i in range(12)]  # distinct per index
    coordinator.snapshots.add(
        {
            "run_at": pin_start.isoformat(),
            "start": pin_start.isoformat(),
            "n": 12,
            "horizon_hours": 12,
            "total_cost": 1.0,
            "soc": soc_traj,
            "grid": [0.0] * 12,
            "dischg": [0.0] * 12,
            "ev": [0.0] * 12,
            "charge": [0.0] * 12,
            "mode": ["c"] * 12,  # the pinned plan charges every hour
        }
    )

    result = await coordinator.get_series(past_hours=8, forecast_lead=6)
    past = {h["start"]: h for h in result["hours"] if h["is_past"]}
    pin_origin = coordinator.snapshots._key(pin_start)

    # Every covered past hour reads the pinned plan's own SoC and shares its origin,
    # and the inverter-mode band follows that plan's schedule (not realized mode).
    for k in range(0, 6):
        h = past.get((pin_start + timedelta(hours=k)).isoformat())
        assert h is not None and h["forecast"] is not None
        assert h["forecast"]["soc_end"] == soc_traj[k]
        assert h["forecast_origin"] == pin_origin
        assert h["inverter_mode"] == "charge"

    # An hour before the pinned plan was made is not covered → no prognoza, and
    # the hour keeps its realized view (mode falls back to the measured flows —
    # passthrough here, with no battery sensors in the fixture).
    before = past.get((pin_start - timedelta(hours=1)).isoformat())
    assert before is not None and before["forecast"] is None
    assert before["inverter_mode"] == "passthrough"


async def test_series_can_pin_exact_forecast_run_at(hass: HomeAssistant) -> None:
    """The chart can pin the prognoza to the vintage in force at a moment.

    ``forecast_run_at`` picks the newest plan made at or before the given
    datetime — even when a newer vintage exists — and reports the pinned
    plan's coverage window so the panel can mark it on the chart.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    selected_start = now - timedelta(hours=8)
    newer_start = now - timedelta(hours=3)
    for run_at, soc in ((selected_start, 77.0), (newer_start, 12.0)):
        coordinator.snapshots.add(
            {
                "run_at": run_at.isoformat(),
                "start": run_at.isoformat(),
                "n": 14,
                "horizon_hours": 14,
                "total_cost": 1.0,
                "soc": [soc] * 14,
                "grid": [0.0] * 14,
                "dischg": [0.0] * 14,
                "ev": [0.0] * 14,
                "charge": [0.0] * 14,
                "mode": ["p"] * 14,
            }
        )

    selected_key = coordinator.snapshots._key(selected_start)
    # Pick a moment BETWEEN the two vintages → the older one was in force.
    picked = (selected_start + timedelta(hours=1, minutes=30)).isoformat()
    result = await coordinator.get_series(past_hours=10, forecast_run_at=picked)

    covered = [
        h
        for h in result["hours"]
        if h.get("forecast_origin") == selected_key and h.get("forecast")
    ]
    assert covered
    assert all(h["forecast"]["soc_end"] == 77.0 for h in covered)

    pin = result["forecast_pin"]
    assert pin is not None
    assert pin["run_at"] == selected_key
    assert pin["start"] == selected_start.isoformat()
    assert pin["end"] == (selected_start + timedelta(hours=14)).isoformat()


async def test_series_current_hour_mode_follows_the_plan_decision(
    hass: HomeAssistant,
) -> None:
    """The in-progress hour shows the plan's inverter mode.

    Regression: the current hour derived its mode from the measured battery
    flows — a few minutes into the hour the 5-minute statistics sit under the
    noise threshold, so the chart said "passthrough" while the inverter-mode
    sensor (the decision) said "discharge".
    """
    from custom_components.powerpilot.const import InverterMode

    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    plan = coordinator.data
    assert plan is not None and plan.decisions
    plan.decisions[0].inverter_mode = InverterMode.DISCHARGE

    result = await coordinator.get_series(past_hours=1)
    current = next(h for h in result["hours"] if h.get("partial"))
    assert current["inverter_mode"] == InverterMode.DISCHARGE


async def test_series_current_hour_carries_the_hour_start_plan(
    hass: HomeAssistant,
) -> None:
    """The in-progress hour exposes the plan it STARTED with (its vintage).

    The live decision can be a mid-hour re-plan covering only the remainder of
    the hour; the plan-vs-real card needs the full-hour baseline (charge/EV/
    grid targets and the planned inverter mode) recorded as the hour began.
    """
    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    # Setup's first refresh recorded this hour's vintage.
    assert coordinator._last_snapshot_hour is not None

    result = await coordinator.get_series(past_hours=1)
    current = next(h for h in result["hours"] if h.get("partial"))
    plan_side = current["hour_plan"]
    assert plan_side is not None
    for key in (
        "charge",
        "discharge",
        "ev",
        "grid",
        "consumption",
        "soc_end",
        "charge_power_kw",
    ):
        assert key in plan_side
    assert current["planned_mode"] in ("charge", "discharge", "passthrough")


async def test_series_future_forecast_chains_from_current_decision(
    hass: HomeAssistant,
) -> None:
    """The first FUTURE hour's forecast starts at the plan's end-of-current-hour.

    Regression: it seeded from the live SoC readings instead — energy planned
    for the remainder of the running hour (e.g. EV charging that hasn't
    started yet) then surfaced as a phantom "21 → 28 %" jump on the next
    hour's tooltip although that hour had no charging planned at all.
    """
    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    plan = coordinator.data
    assert plan is not None and plan.decisions
    plan.decisions[0].battery_soc = 77.7  # planned end of the current hour

    result = await coordinator.get_series(past_hours=1)
    future = next(
        h for h in result["hours"] if not h["is_past"] and h.get("forecast")
    )
    assert future["forecast"]["soc_start"] == 77.7
    assert future["battery_soc_start"] == 77.7


async def test_series_no_vintage_hides_forecast(hass: HomeAssistant) -> None:
    """A past hour with no backing plan shows no "prognoza" — only real data.

    The learned consumption profile is always available (weekday+hour keyed), so
    without gating it would masquerade as a forecast for dates no plan ever ran
    (e.g. before the addon was installed, or older than snapshot retention).
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    await _setup(hass)
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    # Force a "learned" profile so the consumption forecast would be non-empty.
    coordinator.consumption.base.mark_date_observed(now.date())
    coordinator.consumption.base_value = lambda wd, hr: 0.5

    start = now - timedelta(days=40)  # well before any snapshot vintage
    result = await coordinator.get_series(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    past = [h for h in result["hours"] if h["is_past"]]
    assert past, "expected past hours in the window"
    for h in past:
        assert h["forecast"] is None  # no plan → no prognoza side at all
        assert h["forecast_origin"] is None
        # The learned profile must not leak in as a forecast either.
        assert h["consumption_forecast"] is None


async def test_ws_prices_archive(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/prices"})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert "date" in result
    # A full day is always 24 hourly rows with the archive column shape.
    assert len(result["hours"]) == 24
    row = result["hours"][0]
    for key in (
        "start",
        "type",
        "source",
        "fetched_at",
        "energy_price_kwh",
        "distribution_price_kwh",
        "total_price_kwh",
        "estimate_breakdown",
    ):
        assert key in row

    # An explicit (future) date is accepted and echoed back.
    await client.send_json(
        {"id": 2, "type": "powerpilot/prices", "date": "2030-01-01"}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["date"] == "2030-01-01"
    assert len(msg["result"]["hours"]) == 24


async def test_ws_accuracy(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json(
        {"id": 1, "type": "powerpilot/accuracy", "lead_hours": 24, "days": 3}
    )
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert result["lead_hours"] == 24
    assert len(result["bias_by_hour"]) == 24
    assert "mae" in result
    assert "bias" in result
    # Price-forecast accuracy (vs settled certain prices) ships alongside.
    assert "price_mae" in result
    assert "price_bias" in result
    assert "price_samples" in result
    assert "hours" in result


async def test_ws_diagnostics(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/diagnostics"})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert "ready" in result
    assert set(result["summary"]) >= {"ok", "warn", "error", "skip"}
    assert result["groups"], "diagnostics must report at least one group"
    # Every item carries a status verdict and a human message.
    for group in result["groups"]:
        for item in group["items"]:
            assert item["status"] in ("ok", "warn", "error", "skip")
            assert item["message"]
