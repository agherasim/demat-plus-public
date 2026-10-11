"""ADR 0014 spec §6/§6.3a: `band_segments()` probed from `lookup.py
classify`, and `median_zone()`'s per-version PR-scale edges (the single
cited exception being the DEMAT 5+ constant). Requires the knowledge base;
skipped when it is absent, same pattern as `test_report_norms.py`.
"""

from __future__ import annotations

import itertools

import pytest

from demat import config
from demat.reports import norms

requires_kb = pytest.mark.skipif(
    not config.KNOWLEDGE_BASE_DIR.exists(),
    reason=f"knowledge base absent: {config.KNOWLEDGE_BASE_DIR}",
)

# The exact segment edges are the manuals' published bands; they live only in
# the private knowledge base, and so do the tests that pin them (ADR 0017).
# Here: structure only.


@requires_kb
@pytest.mark.parametrize(
    "norm_test", ["demat1plus", "demat2plus", "demat3plus", "demat5plus"]
)
def test_band_segments_follow_manual_category_order(norm_test):
    segments = norms.band_segments(norm_test)
    assert [s.category for s in segments] == norms.CATEGORIES_ORDERED[norm_test]
    for s in segments:
        assert s.label


@requires_kb
def test_band_segments_cover_the_whole_probed_range():
    for norm_test in ("demat1plus", "demat2plus", "demat3plus", "demat5plus"):
        segments = norms.band_segments(norm_test)
        assert segments[0].open_low is True
        assert segments[-1].open_high is True
        # Contiguous: each segment's hi is either the next segment's lo
        # (a shared boundary value, e.g. DEMAT 5+'s T 30/40/60/70 — spec
        # R-13) or one less than it (a clean-edged version, e.g. PR-basis
        # 2+/3+ and T-basis 1+).
        for a, b in itertools.pairwise(segments):
            assert b.lo in (a.hi, a.hi + 1)


@requires_kb
def test_band_segments_is_cached():
    a = norms.band_segments("demat2plus")
    b = norms.band_segments("demat2plus")
    assert a is b


@requires_kb
@pytest.mark.parametrize("norm_test", ["demat2plus", "demat3plus"])
def test_median_zone_is_the_durchschnittlich_segment(norm_test):
    segment = next(
        s for s in norms.band_segments(norm_test) if s.category == norms.MEDIAN_CATEGORY
    )
    assert norms.median_zone(norm_test) == (segment.lo, segment.hi)


def test_median_zone_demat5plus_is_the_cited_constant():
    # Does not need the KB: this is the documented exception (spec §6.3a).
    assert norms.median_zone("demat5plus") == norms.DEMAT5PLUS_PR_AVERAGE
    assert norms.DEMAT5PLUS_PR_AVERAGE == (15.8, 84.5)


def test_median_zone_demat1plus_is_none():
    # Known asymmetry (spec §6.3a) — no PR equivalent in the KB, none
    # estimated. Independent of KB availability.
    assert norms.median_zone("demat1plus") is None


def test_confidence_level_is_90_percent_only_for_5plus():
    assert norms.CONFIDENCE_LEVEL["demat5plus"] == "90 %"
    assert norms.CONFIDENCE_LEVEL["demat1plus"] == "68 %"
    assert norms.CONFIDENCE_LEVEL["demat2plus"] == "68 %"
    assert norms.CONFIDENCE_LEVEL["demat3plus"] == "68 %"


def test_gesamt_status_caveat_none_when_no_status():
    assert norms.gesamt_status_caveat(None) is None


def test_gesamt_status_caveat_unsicher():
    text = norms.gesamt_status_caveat("unsicher", rw=12)
    assert text is not None
    assert "unsicher" in text
    assert "Rohwert 12" in text


def test_gesamt_status_caveat_druckfehler_vermutet():
    text = norms.gesamt_status_caveat("druckfehler_vermutet")
    assert text is not None
    assert "Druckfehler" in text
