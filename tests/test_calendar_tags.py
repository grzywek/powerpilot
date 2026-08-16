"""The calendar tag vocabulary.

Every steering instruction a calendar event can carry is a ``#tag`` in the
summary. Two shapes:

* bare ``#stem`` — a switch (``#ignore``, ``#continue``, ``#kotek``, ``#ess``)
* ``#stem_socNN`` — a SoC deadline for the thing ``stem`` names

The stem for the car comes from ``ev_calendar_keyword`` (default "Kotek"), so
the tag reads ``#kotek_soc100``; the house battery is always ``#ess``.
"""

from __future__ import annotations

import pytest

from custom_components.powerpilot.modules.calendar import (
    has_tag,
    soc_tag,
    strip_tags,
)


@pytest.mark.parametrize(
    ("summary", "stem", "expected"),
    [
        ("#kotek_soc100", "kotek", 100.0),
        ("Babcia #kotek_soc100", "kotek", 100.0),
        ("#KOTEK_SOC100", "kotek", 100.0),  # tags are case-insensitive
        ("#kotek_soc87,5", "kotek", 87.5),  # comma decimal, as in the locale
        ("#kotek_soc87.5", "kotek", 87.5),
        ("#kotek_soc100%", "kotek", 100.0),  # a stray % is tolerated
        ("#kotek_soc120", "kotek", 100.0),  # clamped
        ("#ess_soc80 #kotek_soc100", "ess", 80.0),  # both on one event
        ("#ess_soc80 #kotek_soc100", "kotek", 100.0),
        ("#kotek", "kotek", None),  # bare tag is not a SoC target
        ("#kotek_soc100", "ess", None),  # different stem
        ("Babcia", "kotek", None),
    ],
)
def test_soc_tag(summary: str, stem: str, expected: float | None) -> None:
    assert soc_tag(summary, stem) == expected


@pytest.mark.parametrize(
    ("summary", "stem", "expected"),
    [
        ("#kotek", "kotek", True),
        ("Ładowanie #kotek wieczorem", "kotek", True),
        ("#KOTEK", "kotek", True),
        ("#ess", "ess", True),
        ("#ignore", "ignore", True),
        ("#continue", "continue", True),
        # A SoC tag must NOT read as the bare switch — "#kotek_soc100" means
        # "be at 100 % by then", not "charge flat out for this event".
        ("#kotek_soc100", "kotek", False),
        ("#kotek", "ess", False),
        ("Babcia", "kotek", False),
        # A bare word without the hash is not a tag (the old prefix syntax).
        ("Kotek 100%", "kotek", False),
    ],
)
def test_has_tag(summary: str, stem: str, expected: bool) -> None:
    assert has_tag(summary, stem) is expected


def test_old_prefix_syntax_is_gone() -> None:
    """``Kotek 100%`` was replaced by ``#kotek_soc100`` — no silent fallback."""
    assert soc_tag("Kotek 100%", "kotek") is None
    assert has_tag("Kotek", "kotek") is False


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("Babcia #kotek_soc100", "Babcia"),
        ("Babcia #continue #ess_soc80", "Babcia"),
        ("#ignore Trening", "Trening"),
        ("Wokal  #kotek  ", "Wokal"),
        ("Babcia", "Babcia"),
        # A hashtag that is not a known stem stays — it is part of the title.
        ("Babcia #urodziny", "Babcia #urodziny"),
    ],
)
def test_strip_tags_cleans_the_label(summary: str, expected: str) -> None:
    assert strip_tags(summary, ("ignore", "continue", "ess", "kotek")) == expected
