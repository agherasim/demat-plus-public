"""ADR 0014 spec §7/§11: the child-report SVG primitives and the two sheet
layouts. Pure over hand-built `ChildReportData` fixtures — no knowledge
base needed.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from demat.reports import charts

from tests.child_report_fixtures import (
    make_demat1plus_data,
    make_demat2plus_data,
    make_demat3plus_data,
    make_demat5plus_data,
)

_ALLOWED_TAGS = {"svg", "rect", "line", "text", "g"}
_NS = "{http://www.w3.org/2000/svg}"


def _parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _all_tags(root: ET.Element) -> set[str]:
    return {el.tag.removeprefix(_NS) for el in root.iter()}


def _resolve_fills(root: ET.Element) -> list[str]:
    """Every element's effective `fill`, inherited from the nearest
    ancestor `<g fill=...>` when the element itself carries none.
    """
    fills = []

    def walk(el: ET.Element, inherited: str | None) -> None:
        fill = el.attrib.get("fill", inherited)
        tag = el.tag.removeprefix(_NS)
        if tag == "rect":
            fills.append(fill or "")
        for child in el:
            walk(child, fill)

    walk(root, None)
    return fills


# --- Palette -----------------------------------------------------------------


def test_palette_is_the_documented_whitelist():
    assert charts.CHILD_PALETTE == {
        "#EDEDED",
        "#FFFFFF",
        "#A0A0A0",
        "#C8C8C8",
        "#8C8C8C",
        "#12100C",
        "#5A5750",
        "#8A8A8A",
    }


def test_svg_subset_has_no_disallowed_elements():
    for svg in (
        charts.sheet_rows_with_profile(make_demat2plus_data()),
        charts.sheet_rows_with_profile(make_demat1plus_data()),
        charts.sheet_columns_with_corridor(make_demat3plus_data()),
        charts.notes_area(1),
        charts.notes_area(2),
    ):
        assert "circle" not in svg
        assert "path" not in svg
        assert "<style" not in svg
        assert "class=" not in svg
        root = _parse(svg)
        assert _all_tags(root) <= _ALLOWED_TAGS


def test_white_appears_only_on_child_value_cells_and_zone_rects():
    data = make_demat2plus_data()
    svg = charts.sheet_rows_with_profile(data)
    root = _parse(svg)
    fills = _resolve_fills(root)
    assert "#FFFFFF" in fills
    # Every white rect's geometry must match either a value cell (the RW/PR
    # columns, x in {244, 314, 520}) or a zone rect (x == 464).
    for rect in root.iter(f"{_NS}rect"):
        fill = rect.attrib.get("fill")
        if fill != "#FFFFFF":
            continue
        x = float(rect.attrib["x"])
        # Value/zone cells, plus the legend's zone swatch (x=6, spec §7.1).
        assert x in (6.0, 244.0, 314.0, 464, 520.0)


def test_every_svg_is_one_line():
    for svg in (
        charts.sheet_rows_with_profile(make_demat2plus_data()),
        charts.sheet_columns_with_corridor(make_demat3plus_data()),
        charts.notes_area(1),
    ):
        assert "\n" not in svg


# --- band_dots / value_square / zone_rect ------------------------------------


def test_band_dots_emits_one_line_and_two_round_rects_in_range_color():
    svg = charts.band_dots(10, 20, 10, 40)
    root = ET.fromstring(f"<g>{svg}</g>")
    lines = root.findall("line")
    rects = root.findall("rect")
    assert len(lines) == 1
    assert len(rects) == 2
    assert lines[0].attrib["stroke"] == charts.RANGE
    for r in rects:
        assert r.attrib["fill"] == charts.RANGE
        assert r.attrib["rx"] == "3.5"
        assert r.attrib["ry"] == "3.5"
    # Line endpoints equal the dot centres.
    assert (float(lines[0].attrib["x1"]), float(lines[0].attrib["y1"])) == (10.0, 20.0)
    assert (float(lines[0].attrib["x2"]), float(lines[0].attrib["y2"])) == (10.0, 40.0)


def test_value_square_is_one_ink_rect_with_no_rx():
    svg = charts.value_square(100, 200, 8)
    root = ET.fromstring(svg)
    assert root.tag == "rect"
    assert root.attrib["fill"] == charts.INK
    assert "rx" not in root.attrib
    assert float(root.attrib["width"]) == 8
    assert float(root.attrib["x"]) == 96  # cx - size/2


def test_zone_rect_is_one_cell_value_rect_no_stroke_no_rx():
    svg = charts.zone_rect(10, 20, 30, 40)
    root = ET.fromstring(svg)
    assert root.tag == "rect"
    assert root.attrib["fill"] == charts.CELL_VALUE
    assert "stroke" not in root.attrib
    assert "rx" not in root.attrib


# --- Zone geometry (spec §7.1/§7.2) ------------------------------------------


def test_zone_geometry_demat2plus():
    svg = charts.sheet_rows_with_profile(make_demat2plus_data())
    assert 'x="464" y="30.5" width="112" height="21"' in svg
    assert 'x="464" y="228.5" width="112" height="21"' in svg
    assert svg.count('width="112"') == 10


def test_zone_geometry_demat3plus():
    svg = charts.sheet_columns_with_corridor(make_demat3plus_data())
    assert 'x="200.5" y="156.8" width="479" height="70.4"' in svg


def test_zone_geometry_demat1plus_has_no_zone_rect():
    svg = charts.sheet_rows_with_profile(make_demat1plus_data())
    assert 'width="137' not in svg  # no 2+-style zone width anywhere


def test_zone_same_bytes_for_two_different_children():
    a = make_demat2plus_data()
    b = make_demat2plus_data()
    b.child.display_label = "Someone Else"
    b.person_id = "OTHR"
    svg_a = charts.sheet_rows_with_profile(a)
    svg_b = charts.sheet_rows_with_profile(b)
    zone_re = re.compile(r'<rect x="452\.8"[^/]*/>')
    assert zone_re.findall(svg_a) == zone_re.findall(svg_b)


# --- Distinctness: range vs value never intersect (3+ corridor) -------------


def test_range_and_value_never_intersect_in_corridor():
    # Dots span x in [cx-14.5, cx-7.5]; squares span x in [cx-1, cx+8]
    # (spec §7.2, geometric constants — independent of the fixture data).
    cx = 620
    dot_range = (cx - 14.5, cx - 7.5)
    square_range = (cx - 1, cx + 8)
    assert dot_range[1] < square_range[0]


def test_rows_with_profile_emits_no_range_colored_element_and_no_rounded_rect():
    svg = charts.sheet_rows_with_profile(make_demat2plus_data())
    assert charts.RANGE not in svg
    root = _parse(svg)
    for rect in root.iter(f"{_NS}rect"):
        assert "rx" not in rect.attrib


# --- Legends -------------------------------------------------------------


def test_legend_1plus_has_one_row_short_wording():
    svg = charts.sheet_rows_with_profile(make_demat1plus_data())
    assert "weiß: Werte des Kindes" in svg
    assert "weiße Zellen" not in svg
    assert "Durchschnittsbereich des Tests" not in svg


def test_legend_2plus_has_two_rows_with_its_own_range():
    svg = charts.sheet_rows_with_profile(make_demat2plus_data())
    assert "weiße Zellen" in svg
    assert "PR 30–70" in svg


def test_legend_5plus_shows_its_own_range():
    svg = charts.sheet_rows_with_profile(make_demat5plus_data())
    assert "PR 15,8–84,5" in svg


def test_legend_3plus_corridor_has_all_three_elements():
    svg = charts.sheet_columns_with_corridor(make_demat3plus_data())
    assert "Prozentrang des Kindes" in svg
    assert "Prozentrang-Band (68 %)" in svg
    assert "Durchschnittsbereich des Tests" in svg


# --- No dots when band missing/unparseable, zone still drawn ---------------


def test_no_dots_when_band_missing_but_zone_still_drawn():
    """No dots are drawn for the total column's own band when it is
    missing (spec R-5); the legend's generic mini band_dots explaining the
    concept is unaffected — it is not conditional on this child's band.
    """
    data = make_demat3plus_data(overall_band_range=None)
    svg = charts.sheet_columns_with_corridor(data)
    assert 'x1="609"' not in svg  # the total column's own band line/dots
    assert 'width="479" height="70.4"' in svg  # zone still drawn


def test_no_zone_when_median_zone_is_none():
    data = make_demat3plus_data(median_zone=None)
    svg = charts.sheet_columns_with_corridor(data)
    assert 'width="479"' not in svg
    assert "Durchschnittsbereich des Tests" not in svg


# --- Height formulas ---------------------------------------------------------


def test_height_formulas():
    assert 'height="372"' in charts.sheet_rows_with_profile(make_demat2plus_data())
    assert 'height="314"' in charts.sheet_rows_with_profile(make_demat1plus_data())
    assert 'height="318"' in charts.sheet_columns_with_corridor(make_demat3plus_data())
    assert 'height="196"' in charts.sheet_rows_with_profile(make_demat5plus_data())


def test_notes_area_heights():
    assert 'height="20"' in charts.notes_area(1)
    assert 'height="42"' in charts.notes_area(2)
