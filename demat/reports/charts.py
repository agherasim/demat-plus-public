"""Stage 5a — charts (ADR 0008 spec §6.5). Pure `str`-returning functions
over already-computed numbers; no Streamlit. Inline `<svg>` only, in the
conservative subset: `<svg>`, `<rect>`, `<line>`, `<text>`, `<g>` with
inline presentation attributes only — no `<style>`, no CSS classes, no
`<defs>`/gradients/filters, no external fonts, no `<foreignObject>`. Every
`<svg>` carries explicit `width`, `height`, `viewBox`.

Palette: the app's `--demat-*` design tokens and the brand palette (Coolors
export, palette.pdf), emitted as literal hex so colour survives outside the
app's CSS (`app.py:41-54` is the source of the design tokens). The worst
-> best ramp below runs red -> orange -> gold (brand primary) -> light
blue -> blue (brand secondary) -> green, mirroring the app's semantic
red/green (destructive/ok) anchors at the ends.
"""

from __future__ import annotations

import itertools
from xml.sax.saxutils import escape

from demat.reports.child_data import SHEET_LAYOUT, ChildReportData

# Worst -> best, six-stop ramp. A version with fewer categories samples
# evenly from this ramp (see `category_color`).
_PALETTE = [
    "#B72414",  # red — weakest
    "#F2A016",  # orange
    "#FCB801",  # demat-primary (brand gold)
    "#90B1DE",  # light blue
    "#0062AD",  # demat-secondary
    "#188D42",  # green — best
]
_NEUTRAL = "#8A8A8A"  # no classification available (e.g. T-basis subtest bar)


def category_color(ordinal_rank: int, total_categories: int) -> str:
    """Map a 0-indexed worst->best ordinal rank, out of `total_categories`,
    onto the fixed six-stop palette — sampled evenly so a five-category
    version (1+/5+) still spans red to green.
    """
    if total_categories <= 1:
        return _PALETTE[0]
    scaled = round(ordinal_rank * (len(_PALETTE) - 1) / (total_categories - 1))
    return _PALETTE[max(0, min(scaled, len(_PALETTE) - 1))]


def subtest_bar_chart(
    rows: list[dict],
    *,
    width: int = 520,
    row_height: int = 28,
    label_width: int = 160,
    track_width: int = 260,
) -> str:
    """Horizontal bars, one per subtest: name + printed_label underneath,
    fixed track width, a tick at PR 50, value at the right, `(n=...)` when
    sparse. `rows`: dicts with `name`, `printed_label`, `mean_pr`,
    `n_present`, `n_children`, `color` (already resolved by the caller via
    `category_color`, or None for an uncoloured/neutral bar).
    """
    height = row_height * len(rows) + 20
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'xmlns="http://www.w3.org/2000/svg">'
    ]
    track_x = label_width
    for i, row in enumerate(rows):
        y = 10 + i * row_height
        mid_y = y + row_height // 2
        label = escape(
            f"{row['name']} ({row['printed_label']})"
            if row.get("printed_label")
            else row["name"]
        )
        parts.append(
            f'<text x="4" y="{mid_y + 4}" font-size="12" fill="#12100C">{label}</text>'
        )
        parts.append(
            f'<rect x="{track_x}" y="{y + 4}" width="{track_width}" height="{row_height - 12}" '
            f'fill="#DEDBD5" stroke="none"/>'
        )
        mean_pr = row.get("mean_pr")
        if mean_pr is not None:
            bar_w = max(1, round(track_width * min(mean_pr, 100) / 100))
            color = row.get("color") or _NEUTRAL
            parts.append(
                f'<rect x="{track_x}" y="{y + 4}" width="{bar_w}" height="{row_height - 12}" '
                f'fill="{color}" stroke="none"/>'
            )
            value_label = f"{mean_pr:g}"
            if row.get("n_present") is not None and row.get("n_children") is not None:
                if row["n_present"] < row["n_children"]:
                    value_label += f" (n={row['n_present']})"
        else:
            value_label = "—"
        parts.append(
            f'<text x="{track_x + track_width + 8}" y="{mid_y + 4}" font-size="12" '
            f'fill="#12100C">{escape(value_label)}</text>'
        )
        # PR-50 tick.
        tick_x = track_x + round(track_width * 0.5)
        parts.append(
            f'<line x1="{tick_x}" y1="{y}" x2="{tick_x}" y2="{y + row_height - 4}" '
            'stroke="#12100C" stroke-width="1" stroke-dasharray="2,2"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def child_sparkline(
    series: list[dict],
    *,
    height: int = 48,
    bar_width: int = 16,
    gap: int = 4,
) -> str:
    """The mini chart: one bar per subtest, height proportional to PR,
    coloured by category, NULL as a dashed baseline tick. `series`: ordered
    list of dicts with `key`, `pr`, `color` (None for a NULL PR). First/last
    subtest keys are printed as end labels, flush with their own bar — a
    version with few subtests (e.g. DEMAT 5+'s three) sizes the canvas to
    its actual bar count instead of padding to a fixed width, which used to
    strand the last bar and its label far from the visible content.
    """
    baseline_y = height - 12
    max_bar_h = baseline_y - 6
    total_width = len(series) * (bar_width + gap) + gap
    parts = [
        f'<svg width="{total_width}" height="{height}" viewBox="0 0 {total_width} {height}" '
        'xmlns="http://www.w3.org/2000/svg">'
    ]
    parts.append(
        f'<line x1="0" y1="{baseline_y}" x2="{total_width}" y2="{baseline_y}" '
        'stroke="#DEDBD5" stroke-width="1"/>'
    )
    for i, item in enumerate(series):
        x = gap + i * (bar_width + gap)
        pr = item.get("pr")
        if pr is None:
            parts.append(
                f'<line x1="{x}" y1="{baseline_y}" x2="{x + bar_width}" y2="{baseline_y}" '
                'stroke="#8A8A8A" stroke-width="2" stroke-dasharray="3,2"/>'
            )
            continue
        bar_h = max(2, round(max_bar_h * min(pr, 100) / 100))
        color = item.get("color") or _NEUTRAL
        y = baseline_y - bar_h
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" fill="{color}" stroke="none"/>'
        )
    if series:
        parts.append(
            f'<text x="0" y="{height - 2}" font-size="9" fill="#12100C">'
            f"{escape(series[0]['key'])}</text>"
        )
        last_x = gap + (len(series) - 1) * (bar_width + gap) + bar_width
        parts.append(
            f'<text x="{last_x}" y="{height - 2}" font-size="9" fill="#12100C" '
            f'text-anchor="end">{escape(series[-1]["key"])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# --- Child report (ADR 0014 spec §7) ----------------------------------------
#
# Grayscale palette. `CELL_VALUE` (white) has exactly two uses across the
# whole report: (a) table cells holding the child's own value, and (b) the
# average zone of a diagram's PR axis — never both meanings in the same
# place (spec §7, palette table).

CELL_BG = "#EDEDED"
CELL_VALUE = "#FFFFFF"
BORDER = "#A0A0A0"
GRID = "#C8C8C8"
RANGE = "#8C8C8C"
INK = "#12100C"
MUTED = "#5A5750"
RULE = "#8A8A8A"

CHILD_PALETTE = {CELL_BG, CELL_VALUE, BORDER, GRID, RANGE, INK, MUTED, RULE}


def _n(v: float) -> str:
    """Trim a coordinate/size to the shortest exact decimal (matches the
    worked examples: `137.2`, `30.5`, `424.24`, never `137.20` or `30.0`).
    """
    return f"{v:g}"


def band_dots(x1: float, y1: float, x2: float, y2: float) -> str:
    """The child's band (spec §7.0): a line between the two edge centres,
    plus a 7 px round dot centred on each edge. Used only by the DEMAT 3+
    corridor and its legend — no other diagram draws a range.
    """
    parts = [
        f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
        f'stroke="{RANGE}" stroke-width="2"/>'
    ]
    for x, y in ((x1, y1), (x2, y2)):
        parts.append(
            f'<rect x="{_n(x - 3.5)}" y="{_n(y - 3.5)}" width="7" height="7" '
            f'rx="3.5" ry="3.5" fill="{RANGE}"/>'
        )
    return "".join(parts)


def value_square(cx: float, cy: float, size: float) -> str:
    """The child's value (spec §7.0): one sharp-cornered square centred on
    `(cx, cy)`. 8 px in the rows layout, 9 px in the corridor.
    """
    half = size / 2
    return (
        f'<rect x="{_n(cx - half)}" y="{_n(cy - half)}" width="{_n(size)}" '
        f'height="{_n(size)}" fill="{INK}"/>'
    )


def zone_rect(x: float, y: float, width: float, height: float) -> str:
    """One rectangle of the average ("durchschnittlich") zone (spec §7.0):
    plain, unstroked, `CELL_VALUE`. The corridor uses one; the profile
    layouts use one per subtest row, inset so row borders stay visible.
    """
    return (
        f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(width)}" '
        f'height="{_n(height)}" fill="{CELL_VALUE}"/>'
    )


def notes_area(lines: int) -> str:
    """ "Bemerkungen:" plus 1 or 2 ruled lines (spec §7.3)."""
    height = 20 if lines == 1 else 42
    parts = [
        f'<svg width="680" height="{height}" viewBox="0 0 680 {height}" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<text x="0" y="12" font-size="10" font-weight="bold" fill="{INK}">'
        "Bemerkungen:</text>",
        f'<line x1="82" y1="14" x2="680" y2="14" stroke="{RULE}" stroke-width="1"/>',
    ]
    if lines == 2:
        parts.append(
            f'<line x1="0" y1="36" x2="680" y2="36" stroke="{RULE}" stroke-width="1"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _x_pr(v: float) -> float:
    return 380 + 2.8 * v


def sheet_rows_with_profile(data: ChildReportData) -> str:
    """DEMAT 1+/2+, provisionally 5+ (spec §7.1): one row per subtest, a
    "Profil PR" plotting subtest PRs only, and the Gesamtwert block as
    plain grey/white cells — no marker, dot or line is drawn for the
    overall value or its band (spec R-... / OQ-12: only the sheet's own
    printed numbers).
    """
    n = len(data.subtests)
    row_h = 22 if n <= 10 else 220 // n
    layout = SHEET_LAYOUT[data.norm_test]
    gesamt_fields = data.gesamt_fields
    k = len(gesamt_fields)
    has_zone = data.median_zone is not None
    legend_h = 34 if has_zone else 20
    height = 30 + n * row_h + k * 22 + legend_h
    block_top = 30 + n * row_h

    NAME_X, NAME_W = 0, 200
    KEY_X, KEY_W = 200, 44
    RW_X, RW_W = 244, 70
    PR_X, PR_W = 314, 50
    PROFILE_X, PROFILE_W = 364, 316

    grey: list[str] = [
        f'<rect x="{NAME_X}" y="0" width="{NAME_W}" height="30"/>',
        f'<rect x="{KEY_X}" y="0" width="{KEY_W}" height="30"/>',
        f'<rect x="{RW_X}" y="0" width="{RW_W}" height="30"/>',
        f'<rect x="{PR_X}" y="0" width="{PR_W}" height="30"/>',
        f'<rect x="{PROFILE_X}" y="0" width="{PROFILE_W}" height="30"/>',
    ]
    white: list[str] = []
    zones: list[str] = []
    grid: list[str] = []
    profile_line_points: list[tuple[float, float]] = []
    squares: list[str] = []
    text: list[str] = [
        '<text x="6" y="19" font-weight="bold">Teilbereich</text>',
        f'<text x="{RW_X + 35}" y="13" font-weight="bold" text-anchor="middle">RW</text>',
        f'<text x="{RW_X + 35}" y="25" font-size="8" text-anchor="middle" '
        f'fill="{MUTED}">(max.)</text>',
        f'<text x="{PR_X + 25}" y="13" font-weight="bold" text-anchor="middle">PR</text>',
        f'<text x="{PR_X + 25}" y="25" font-size="8" text-anchor="middle" '
        f'fill="{MUTED}">{layout["pr_source_label"]}</text>',
        f'<text x="{PROFILE_X + 6}" y="12" font-size="9" font-weight="bold">Profil PR</text>',
    ]
    for tick in range(0, 101, 10):
        x = _x_pr(tick)
        text.append(
            f'<text x="{_n(x)}" y="26" font-size="8" text-anchor="middle">{tick}</text>'
        )
        grid.append(f'<line x1="{_n(x)}" y1="30" x2="{_n(x)}" y2="{30 + n * row_h}"/>')

    for i, s in enumerate(data.subtests):
        y = 30 + i * row_h
        mid = y + row_h / 2
        grey.append(f'<rect x="{NAME_X}" y="{y}" width="{NAME_W}" height="{row_h}"/>')
        grey.append(f'<rect x="{KEY_X}" y="{y}" width="{KEY_W}" height="{row_h}"/>')
        grey.append(
            f'<rect x="{PROFILE_X}" y="{y}" width="{PROFILE_W}" height="{row_h}"/>'
        )

        rw_present = not s.rw_display.startswith("—")
        (white if rw_present else grey).append(
            f'<rect x="{RW_X}" y="{y}" width="{RW_W}" height="{row_h}"/>'
        )
        (white if s.pr_is_value else grey).append(
            f'<rect x="{PR_X}" y="{y}" width="{PR_W}" height="{row_h}"/>'
        )

        text.append(f'<text x="6" y="{_n(mid + 4)}">{escape(s.name)}</text>')
        text.append(
            f'<text x="{KEY_X + 22}" y="{_n(mid + 4)}" font-weight="bold" '
            f'text-anchor="middle">{escape(s.key)}</text>'
        )
        text.append(
            f'<text x="{RW_X + 35}" y="{_n(mid + 4)}" text-anchor="middle">'
            f"{escape(s.rw_display)}</text>"
        )
        text.append(
            f'<text x="{PR_X + 25}" y="{_n(mid + 4)}" font-weight="bold" '
            f'text-anchor="middle">{escape(s.pr_display)}</text>'
        )

        if data.median_zone is not None:
            lo, hi = data.median_zone
            zones.append(
                zone_rect(_x_pr(lo), y + 0.5, _x_pr(hi) - _x_pr(lo), row_h - 1)
            )

        if s.pr is not None:
            cx = _x_pr(s.pr)
            profile_line_points.append((cx, mid))
            squares.append(value_square(cx, mid, 8))

    lines: list[str] = []
    for (x1, y1), (x2, y2) in itertools.pairwise(profile_line_points):
        lines.append(f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}"/>')

    # --- Gesamtwert block -------------------------------------------------
    block_h = k * 22
    grey.append(
        f'<rect x="{NAME_X}" y="{block_top}" width="{RW_X}" height="{block_h}"/>'
    )
    total_rw_cell = (
        f'<rect x="{RW_X}" y="{block_top}" width="{RW_W}" height="{block_h}"/>'
    )
    white.append(total_rw_cell)
    text.append(
        f'<text x="6" y="{block_top + 25}" font-weight="bold">Gesamtwert</text>'
    )
    text.append(
        f'<text x="6" y="{block_top + 41}" font-size="9" fill="{MUTED}">Rohwert (ges)</text>'
    )
    text.append(
        f'<text x="{RW_X + 35}" y="{_n(block_top + block_h / 2 + 4)}" font-size="11" '
        f'font-weight="bold" text-anchor="middle">{escape(data.rw_total_display)}</text>'
    )
    for i, field in enumerate(gesamt_fields):
        fy = block_top + i * 22
        grey.append(f'<rect x="{PR_X}" y="{fy}" width="206" height="22"/>')
        (white if field.kind == "value" else grey).append(
            f'<rect x="520" y="{fy}" width="160" height="22"/>'
        )
        text.append(f'<text x="320" y="{fy + 15}">{escape(field.label)}</text>')
        weight = ' font-weight="bold"' if field.kind == "value" else ""
        text.append(
            f'<text x="600" y="{fy + 15}"{weight} text-anchor="middle">'
            f"{escape(field.display)}</text>"
        )

    # --- Legend -------------------------------------------------------
    legend_row1_y = height - legend_h + 6
    legend: list[str] = [value_square(10, legend_row1_y + 4, 8)]
    if has_zone:
        legend_text1 = (
            "Prozentrang des Kindes je Teilbereich · weiße Zellen: "
            "Werte des Kindes · grau: Beschriftungen und Bänder"
        )
    else:
        legend_text1 = (
            "Prozentrang des Kindes je Teilbereich · weiß: Werte des "
            "Kindes · grau: Beschriftungen und Bänder"
        )
    legend.append(
        f'<text x="20" y="{legend_row1_y + 8}" font-size="9" fill="{MUTED}">'
        f"{legend_text1}</text>"
    )
    if has_zone:
        assert data.median_zone is not None
        lo, hi = data.median_zone
        legend_row2_y = legend_row1_y + 14
        legend.append(
            f'<rect x="6" y="{legend_row2_y}" width="18" height="9" fill="{CELL_VALUE}" '
            f'stroke="{BORDER}" stroke-width="1"/>'
        )
        lo_str = f"{lo:.1f}".replace(".0", "").replace(".", ",")
        hi_str = f"{hi:.1f}".replace(".0", "").replace(".", ",")
        legend.append(
            f'<text x="30" y="{legend_row2_y + 8}" font-size="9" fill="{MUTED}">'
            f"weiße Fläche im Profil: Durchschnittsbereich des Tests "
            f"(PR {lo_str}–{hi_str}, „durchschnittlich“) · für "
            "alle Kinder gleich</text>"
        )

    parts = [
        f'<svg width="680" height="{height}" viewBox="0 0 680 {height}" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<g fill="{CELL_BG}" stroke="{BORDER}">{"".join(grey)}</g>',
        f'<g fill="{CELL_VALUE}" stroke="{BORDER}">{"".join(white)}</g>',
    ]
    if zones:
        parts.append(f'<g fill="{CELL_VALUE}">{"".join(zones)}</g>')
    parts.append(f'<g stroke="{GRID}" stroke-width="1">{"".join(grid)}</g>')
    if lines:
        parts.append(f'<g stroke="{INK}" stroke-width="1.2">{"".join(lines)}</g>')
    if squares:
        parts.append(f'<g fill="{INK}">{"".join(squares)}</g>')
    parts.append(f'<g font-size="10" fill="{INK}">{"".join(text)}</g>')
    parts.extend(legend)
    parts.append("</svg>")
    return "".join(parts)


_CORRIDOR_CX = (260, 380, 500, 620)
_CORRIDOR_HEADER_H = 34
_CORRIDOR_ROW_H = 22
_CORRIDOR_PANEL_TOP = 112
_CORRIDOR_PANEL_BOTTOM = 272


def _corridor_y(pr: float) -> float:
    return _CORRIDOR_PANEL_BOTTOM - 1.6 * pr


def sheet_columns_with_corridor(data: ChildReportData) -> str:
    """DEMAT 3+ (spec §7.2): subtest columns plus a Testgesamtwert column,
    a "Prozentrang-Band" corridor panel below, band dots for the total
    column only (spec OQ-7), and the value square in every column with a
    PR.
    """
    subtests = data.subtests
    band_field = data.gesamt_fields[0] if data.gesamt_fields else None
    has_zone = data.median_zone is not None
    height = 318 if has_zone else 304

    grey: list[str] = [f'<rect x="0" y="0" width="200" height="{_CORRIDOR_HEADER_H}"/>']
    for cx in _CORRIDOR_CX:
        grey.append(
            f'<rect x="{cx - 60}" y="0" width="120" height="{_CORRIDOR_HEADER_H}"/>'
        )
    white: list[str] = []
    text: list[str] = []

    for i, cx in enumerate(_CORRIDOR_CX):
        if i < 3:
            title = escape(subtests[i].column_title)
            text.append(
                f'<text x="{cx}" y="14" font-size="8" text-anchor="middle" '
                f'fill="{MUTED}">Subtest</text>'
            )
            text.append(
                f'<text x="{cx}" y="28" font-weight="bold" text-anchor="middle">'
                f"{title}</text>"
            )
        else:
            text.append(
                f'<text x="{cx}" y="14" font-size="8" text-anchor="middle" '
                f'fill="{MUTED}">Test-</text>'
            )
            text.append(
                f'<text x="{cx}" y="28" font-weight="bold" text-anchor="middle">'
                "gesamtwert</text>"
            )

    row_labels = [
        "Rohwert-Summe",
        "Prozentrang",
        f"Prozentrang-Band ({data.confidence_level})",
    ]
    for row_i, label in enumerate(row_labels):
        y = _CORRIDOR_HEADER_H + row_i * _CORRIDOR_ROW_H
        grey.append(f'<rect x="0" y="{y}" width="200" height="{_CORRIDOR_ROW_H}"/>')
        text.append(
            f'<text x="6" y="{y + 15}" font-weight="bold">{escape(label)}</text>'
        )
        for i, cx in enumerate(_CORRIDOR_CX):
            cell_x = cx - 60
            if row_i == 0:
                display = subtests[i].rw_display if i < 3 else data.rw_total_display
                present = not display.startswith("—")
                (white if present else grey).append(
                    f'<rect x="{cell_x}" y="{y}" width="120" height="{_CORRIDOR_ROW_H}"/>'
                )
                text.append(
                    f'<text x="{cx}" y="{y + 15}" text-anchor="middle">'
                    f"{escape(display)}</text>"
                )
            elif row_i == 1:
                if i < 3:
                    display = subtests[i].pr_display
                    present = subtests[i].pr_is_value
                else:
                    display = (
                        f"{data.overall_value:g}"
                        if data.overall_value is not None
                        else "—"
                    )
                    present = data.overall_value is not None
                (white if present else grey).append(
                    f'<rect x="{cell_x}" y="{y}" width="120" height="{_CORRIDOR_ROW_H}"/>'
                )
                weight = ' font-weight="bold"' if present else ""
                text.append(
                    f'<text x="{cx}" y="{y + 15}"{weight} text-anchor="middle">'
                    f"{escape(display)}</text>"
                )
            else:
                grey.append(
                    f'<rect x="{cell_x}" y="{y}" width="120" height="{_CORRIDOR_ROW_H}"/>'
                )
                display = "—"
                if i == 3 and band_field is not None and band_field.present:
                    display = band_field.display
                text.append(
                    f'<text x="{cx}" y="{y + 15}" text-anchor="middle">'
                    f"{escape(display)}</text>"
                )

    _panel_h = _CORRIDOR_PANEL_BOTTOM - _CORRIDOR_PANEL_TOP
    grey.append(
        f'<rect x="0" y="{_CORRIDOR_PANEL_TOP}" width="200" height="{_panel_h}"/>'
    )
    grey.append(
        f'<rect x="200" y="{_CORRIDOR_PANEL_TOP}" width="480" height="{_panel_h}"/>'
    )
    text.append(
        f'<text x="6" y="{_CORRIDOR_PANEL_TOP + 18}" font-weight="bold">'
        "Prozentrang-Band</text>"
    )
    text.append(
        f'<text x="6" y="{_CORRIDOR_PANEL_TOP + 32}" font-size="8" fill="{MUTED}">'
        "Diagramm wie im Testbogen</text>"
    )

    zones: list[str] = []
    if data.median_zone is not None:
        lo, hi = data.median_zone
        zones.append(
            zone_rect(200.5, _corridor_y(hi), 479, _corridor_y(lo) - _corridor_y(hi))
        )

    grid: list[str] = []
    for tick in range(10, 100, 10):
        grid_y = _corridor_y(tick)
        grid.append(f'<line x1="200" y1="{_n(grid_y)}" x2="680" y2="{_n(grid_y)}"/>')
    for cx in _CORRIDOR_CX[:-1]:
        grid.append(
            f'<line x1="{cx + 60}" y1="{_CORRIDOR_PANEL_TOP}" x2="{cx + 60}" '
            f'y2="{_CORRIDOR_PANEL_BOTTOM}"/>'
        )
    for tick in range(0, 101, 20):
        label_y = _corridor_y(tick)
        text.append(
            f'<text x="194" y="{_n(label_y + 3)}" font-size="8" text-anchor="end">{tick}</text>'
        )

    dots: list[str] = []
    if data.overall_band_range is not None:
        lo, hi = data.overall_band_range
        cx = _CORRIDOR_CX[-1]
        dots.append(band_dots(cx - 11, _corridor_y(hi), cx - 11, _corridor_y(lo)))

    squares: list[str] = []
    for i, cx in enumerate(_CORRIDOR_CX):
        pr = subtests[i].pr if i < 3 else data.overall_value
        if pr is None:
            continue
        square_cx = cx + 3.5
        square_cy = _corridor_y(pr)
        if square_cy + 4.5 > _CORRIDOR_PANEL_BOTTOM - 5:
            square_cy = _CORRIDOR_PANEL_BOTTOM - 5 - 4.5
        squares.append(value_square(square_cx, square_cy, 9))
        text.append(
            f'<text x="{cx + 14}" y="{_n(square_cy + 3)}" font-size="9" '
            f'font-weight="bold">PR {pr:g}</text>'
        )

    legend_row1_y = 290
    legend: list[str] = [
        value_square(10.5, legend_row1_y + 0.5, 9),
        f'<text x="20" y="{legend_row1_y + 4}" font-size="9" fill="{MUTED}">'
        "Prozentrang des Kindes</text>",
        band_dots(143.5, legend_row1_y, 161.5, legend_row1_y),
        f'<text x="172" y="{legend_row1_y + 4}" font-size="9" fill="{MUTED}">'
        "Prozentrang-Band (68 %): unterer und oberer Rand · für die "
        "Subtests nicht gespeichert</text>",
    ]
    if has_zone:
        assert data.median_zone is not None
        lo, hi = data.median_zone
        legend_row2_y = 300
        lo_str = f"{lo:.1f}".replace(".0", "").replace(".", ",")
        hi_str = f"{hi:.1f}".replace(".0", "").replace(".", ",")
        legend.append(
            f'<rect x="6" y="{legend_row2_y}" width="18" height="9" fill="{CELL_VALUE}" '
            f'stroke="{BORDER}" stroke-width="1"/>'
        )
        legend.append(
            f'<text x="30" y="{legend_row2_y + 8}" font-size="9" fill="{MUTED}">'
            f"weiße Fläche: Durchschnittsbereich des Tests (PR "
            f"{lo_str}–{hi_str}, „durchschnittlich“) · für "
            "alle Kinder gleich</text>"
        )

    parts = [
        f'<svg width="680" height="{height}" viewBox="0 0 680 {height}" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<g fill="{CELL_BG}" stroke="{BORDER}">{"".join(grey)}</g>',
    ]
    if white:
        parts.append(f'<g fill="{CELL_VALUE}" stroke="{BORDER}">{"".join(white)}</g>')
    parts.extend(zones)
    if grid:
        parts.append(f'<g stroke="{GRID}" stroke-width="1">{"".join(grid)}</g>')
    parts.extend(dots)
    if squares:
        parts.append(f'<g fill="{INK}">{"".join(squares)}</g>')
    parts.append(f'<g font-size="10" fill="{INK}">{"".join(text)}</g>')
    parts.extend(legend)
    parts.append("</svg>")
    return "".join(parts)
