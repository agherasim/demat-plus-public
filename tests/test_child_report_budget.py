"""ADR 0014 spec §8.2: the single-page height budget. The worst case
(DEMAT 2+, 10 rows, k=4, zone, below-average rating, misprint-refusal +
cross-check caveats, both subtest lists full, a wrapping Normtabelle
label) must sit at about 940 of 1032 px and stay under the 95 % (980 px)
test ceiling for every version's own worst case.
"""

from __future__ import annotations

import json
from pathlib import Path

from demat.reports.child_data import ChildReportData
from demat.reports.render import PAGE_BUDGET_PX, estimate_height_px

from tests.child_report_fixtures import (
    make_demat1plus_data,
    make_demat2plus_data,
    make_demat3plus_data,
    make_demat5plus_data,
)

_CEILING = round(PAGE_BUDGET_PX * 0.95)
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "child_report_worst_case.json"


def _worst_case_2plus() -> ChildReportData:
    """The spec §8.2 fixture: DEMAT 2+, 10 rows, k=4, a zone, a below-
    average rating, a misprint-refusal caveat plus a cross-check caveat,
    both subtest lists full, and a wrapping Normtabelle label.
    """
    return ChildReportData.model_validate(json.loads(_FIXTURE_PATH.read_text()))


def test_page_budget_constant():
    assert PAGE_BUDGET_PX == 1032


def test_worst_case_is_about_940_and_under_ceiling():
    height = estimate_height_px(_worst_case_2plus())
    assert 900 <= height <= 980
    assert height <= _CEILING


def test_every_version_worst_case_under_ceiling():
    for data in (
        _worst_case_2plus(),
        make_demat1plus_data(),
        make_demat3plus_data(),
        make_demat5plus_data(),
    ):
        assert estimate_height_px(data) <= _CEILING


def test_more_bullets_increase_the_estimate():
    base = make_demat2plus_data()
    base.hint_lines = ["a", "b", "c"]
    loaded = make_demat2plus_data()
    loaded.hint_lines = ["a", "b", "c", "d", "e", "f"]
    assert estimate_height_px(loaded) > estimate_height_px(base)
