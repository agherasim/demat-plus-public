"""ADR 0014 spec §11: `render_child_markdown()` — structural checks against
hand-built `ChildReportData` fixtures (examples A/B/C's numbers, spec-
matched by construction; see `tests/child_report_fixtures.py`). No golden
byte-diff against the (untracked) appendix file — see the implementation
report for why.
"""

from __future__ import annotations

import re

from demat.reports.render import render_child_markdown

from tests.child_report_fixtures import (
    make_demat1plus_data,
    make_demat2plus_data,
    make_demat3plus_data,
)


def test_every_svg_sits_on_one_line_with_blank_lines_around_it():
    md = render_child_markdown(make_demat2plus_data())
    for match in re.finditer(r"<svg.*?</svg>", md):
        assert "\n" not in match.group(0)


def test_rating_band_table_only_child_column_is_white():
    data = make_demat2plus_data()
    md = render_child_markdown(data)
    # Find the second HTML table (the rating-band table).
    tables = re.findall(r"<table.*?</table>", md, re.DOTALL)
    rating_table = tables[1]
    white_cells = re.findall(r"background:#FFFFFF[^>]*>([^<]*)<", rating_table)
    assert white_cells  # at least one white cell
    for text in white_cells:
        assert text.strip() in (data.child.overall_category, "PR 71–92", "71–92")


def test_footer_shows_id_and_no_name():
    data = make_demat2plus_data()
    md = render_child_markdown(data)
    footer_line = next(line for line in md.splitlines() if line.startswith("<sub>"))
    assert "K7QM" in footer_line
    assert "Lena" not in footer_line
    assert "Bogen #412" in footer_line


def test_footer_unlinked_shows_bogen_only():
    data = make_demat2plus_data()
    data.person_id = None
    md = render_child_markdown(data)
    footer_line = next(line for line in md.splitlines() if line.startswith("<sub>"))
    assert "nicht verknüpft" in footer_line
    assert "Bogen #412" in footer_line


def test_write_in_blank_when_no_display_name():
    data = make_demat2plus_data()
    md = render_child_markdown(data)
    assert "_" * 22 in md


def test_display_name_shown_when_store_raw_names():
    data = make_demat2plus_data()
    data.display_name = "Lena Beispiel"
    md = render_child_markdown(data)
    assert "Lena Beispiel" in md


def test_t_basis_caption_present_for_1plus_absent_for_2plus():
    md_1plus = render_child_markdown(make_demat1plus_data())
    md_2plus = render_child_markdown(make_demat2plus_data())
    assert "beurteilt nur den Gesamtwert" in md_1plus
    assert "beurteilt nur den Gesamtwert" not in md_2plus


def test_1plus_rating_band_table_prefixes_t_not_pr():
    md = render_child_markdown(make_demat1plus_data())
    tables = re.findall(r"<table.*?</table>", md, re.DOTALL)
    assert "T ≤31" in tables[1]


def test_3plus_uses_corridor_layout_svg():
    md = render_child_markdown(make_demat3plus_data())
    assert "Prozentrang-Band" in md
    assert 'height="318"' in md


def test_no_html_blank_lines_inside_tables():
    md = render_child_markdown(make_demat2plus_data())
    for table in re.findall(r"<table.*?</table>", md, re.DOTALL):
        assert "\n\n" not in table


def test_rating_line_shows_no_overall_value_case():
    data = make_demat2plus_data()
    data.overall_value = None
    data.child.overall_category = None
    data.child.overall_categories = None
    md = render_child_markdown(data)
    assert "nicht bestimmbar (kein Normwert erfasst)" in md
    tables = re.findall(r"<table.*?</table>", md, re.DOTALL)
    assert "#FFFFFF" not in tables[1]
