"""Retail price assembly: net cost components → gross per-kWh, seller-style.

Mirrors the Polish energy-bill structure for a single hour::

    prąd z obsługą = TGE (wholesale) + marża (seller markup)   [net]
    dystrybucja    = składnik bazowy + składnik strefowy         [net]
    podatki        = akcyza + VAT on (energy + distribution + akcyza)
    RAZEM (brutto) = prąd + dystrybucja + podatki

The fixed monthly charge (opłata stała / abonamentowa) is intentionally **not**
part of the per-kWh price — sellers bill it as a separate monthly position.

Rounding is configurable so the displayed full price can match a specific
seller's bill to the grosz. The mode is stamped per archived hour by the caller,
so changing it later only affects newly-fetched hours and never rewrites history.
"""

from __future__ import annotations

from .const import (
    PRICE_ROUNDING_NONE,
    PRICE_ROUNDING_PER_BUCKET,
    PRICE_ROUNDING_TOTAL,
)


def assemble(
    *,
    tge: float | None,
    markup: float,
    dist_net: float | None,
    excise: float,
    vat_rate: float,
    rounding: str,
    dist_vat_rate: float | None = None,
) -> dict[str, float | None]:
    """Assemble the gross per-kWh breakdown from net components.

    All money inputs are net PLN/kWh. ``vat_rate`` is a rate (e.g. ``0.23``),
    applied to the energy + excise side; ``dist_vat_rate`` defaults to the same
    rate for the distribution side. ``dist_net`` may be ``None`` when no tariff
    has resolved yet — the distribution bucket and the gross total are then
    ``None`` (we never invent a distribution price).

    Returns a dict with the net components, the combined ``taxes`` bucket
    (akcyza + VAT), and the gross ``total`` (``None`` when distribution is
    missing). ``energy_gross`` / ``distribution_gross`` are the optimizer-facing
    per-side gross prices (akcyza folded into the energy side).
    """
    tge = tge or 0.0
    markup = markup or 0.0
    excise = excise or 0.0
    if dist_vat_rate is None:
        dist_vat_rate = vat_rate

    energy_net = tge + markup

    energy_gross = (energy_net + excise) * (1.0 + vat_rate)
    distribution_gross = (
        None if dist_net is None else dist_net * (1.0 + dist_vat_rate)
    )

    if dist_net is None:
        taxes: float | None = None
        total: float | None = None
    elif rounding == PRICE_ROUNDING_PER_BUCKET:
        # Round each bill bucket independently, then sum — what sellers print.
        taxes = (
            round(vat_rate * energy_net, 2)
            + round(dist_vat_rate * dist_net, 2)
            + round(excise * (1.0 + vat_rate), 2)
        )
        total = round(round(energy_net, 2) + round(dist_net, 2) + taxes, 2)
    elif rounding == PRICE_ROUNDING_TOTAL:
        total = round(energy_gross + distribution_gross, 2)
        taxes = round(total - energy_net - dist_net, 2)
    else:  # PRICE_ROUNDING_NONE
        total = energy_gross + distribution_gross
        taxes = excise + vat_rate * energy_net + dist_vat_rate * dist_net

    return {
        "tge": tge,
        "markup": markup,
        "energy_net": energy_net,
        "distribution_net": dist_net,
        "excise": excise,
        "vat_rate": vat_rate,
        "taxes": taxes,
        "total": total,
        "energy_gross": energy_gross,
        "distribution_gross": distribution_gross,
    }


def energy_gross(tge: float, markup: float, excise: float, vat_rate: float) -> float:
    """Gross energy-side price (PLN/kWh): ``(tge + markup + excise) * (1 + vat)``.

    Akcyza is folded into the energy side so the optimizer prices a single
    gross energy bucket; distribution is priced separately by the tariff module.
    """
    return ((tge or 0.0) + (markup or 0.0) + (excise or 0.0)) * (1.0 + vat_rate)


__all__ = [
    "assemble",
    "energy_gross",
    "PRICE_ROUNDING_PER_BUCKET",
    "PRICE_ROUNDING_TOTAL",
    "PRICE_ROUNDING_NONE",
]
