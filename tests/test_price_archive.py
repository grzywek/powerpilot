"""Unit tests for the energy-price archive (the "Ceny" tab data model)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    ESTIMATE_WEEKLY_WEIGHTS,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_FORECAST,
)
from custom_components.powerpilot.modules.prices import PriceArchive


def _hour():
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)


def test_layering_estimated_forecast_certain() -> None:
    archive = PriceArchive()
    h = _hour()

    # forecast lands first
    assert archive.record(h, 1.0, PRICE_TYPE_FORECAST, "pradcast", "t1") is True
    assert archive.get(h)["type"] == PRICE_TYPE_FORECAST

    # a forecast refresh overwrites value + fetched_at
    assert archive.record(h, 1.2, PRICE_TYPE_FORECAST, "pradcast", "t2") is True
    assert archive.get(h)["energy"] == 1.2
    assert archive.get(h)["fetched_at"] == "t2"

    # certain replaces forecast
    assert archive.record(h, 1.3, PRICE_TYPE_CERTAIN, "pradcast", "t3") is True
    assert archive.get(h)["type"] == PRICE_TYPE_CERTAIN

    # certain is final — a later forecast must NOT downgrade it
    assert archive.record(h, 9.9, PRICE_TYPE_FORECAST, "pradcast", "t4") is False
    assert archive.get(h)["energy"] == 1.3
    assert archive.get(h)["type"] == PRICE_TYPE_CERTAIN


def test_record_is_noop_when_identical() -> None:
    archive = PriceArchive()
    h = _hour()
    assert archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t1") is True
    assert archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t1") is False


def test_refetch_preserves_original_fetched_at() -> None:
    archive = PriceArchive()
    h = _hour()
    archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "pradcast", "13:41")
    # Re-fetching the same certain price (e.g. after a restart) must not bump the
    # "pobrano" timestamp.
    assert archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "pradcast", "14:08") is False
    assert archive.get(h)["fetched_at"] == "13:41"
    # A genuine value change does update the timestamp.
    assert archive.record(h, 1.1, PRICE_TYPE_CERTAIN, "pradcast", "14:08") is True
    assert archive.get(h)["fetched_at"] == "14:08"


def test_estimate_weighted_average() -> None:
    archive = PriceArchive()
    h = _hour()
    archive.record(h - timedelta(days=7), 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h - timedelta(days=14), 2.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h - timedelta(days=21), 3.0, PRICE_TYPE_CERTAIN, "pradcast", "t")

    price, breakdown = archive.estimate(h)
    w1, w2, w3 = ESTIMATE_WEEKLY_WEIGHTS  # (0.5, 0.3, 0.2)
    assert round(price, 6) == round(1.0 * w1 + 2.0 * w2 + 3.0 * w3, 6)
    assert len(breakdown) == 3
    assert [s["weeks_ago"] for s in breakdown] == [1, 2, 3]
    assert all(s["value"] is not None for s in breakdown)


def test_estimate_renormalises_over_available_samples() -> None:
    archive = PriceArchive()
    h = _hour()
    # only weeks 1 and 3 present
    archive.record(h - timedelta(days=7), 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h - timedelta(days=21), 3.0, PRICE_TYPE_CERTAIN, "pradcast", "t")

    price, _ = archive.estimate(h)
    w1, _w2, w3 = ESTIMATE_WEEKLY_WEIGHTS
    expected = (1.0 * w1 + 3.0 * w3) / (w1 + w3)
    assert round(price, 6) == round(expected, 6)


def test_estimate_none_without_history() -> None:
    archive = PriceArchive()
    price, breakdown = archive.estimate(_hour())
    assert price is None
    assert len(breakdown) == 3  # breakdown is always returned for the tooltip


def test_prune_drops_entries_older_than_retention() -> None:
    archive = PriceArchive()
    h = _hour()
    archive.record(h - timedelta(days=200), 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h, 2.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.prune()
    assert archive.get(h - timedelta(days=200)) is None
    assert archive.get(h) is not None


def test_serialisation_roundtrip() -> None:
    archive = PriceArchive()
    h = _hour()
    archive.record(h, 1.5, PRICE_TYPE_FORECAST, "pradcast", "t", p10=1.0, p90=2.0)
    restored = PriceArchive.from_dict(archive.to_dict())
    assert restored.get(h) == archive.get(h)
    assert len(restored) == 1
