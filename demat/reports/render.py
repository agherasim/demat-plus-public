"""Stage 5b — render (ADR 0008 spec §6.5). Jinja2 assembly of the
markdown+inline-SVG artifact string. Deterministic; no LLM, no KB call, no
Streamlit. The same string is (a) handed to
`st.markdown(..., unsafe_allow_html=True)` and (b) written to
`data/reports/<slug>/<timestamp>.md`.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from demat.reports.charts import (
    category_color,
    child_sparkline,
    notes_area,
    sheet_columns_with_corridor,
    sheet_rows_with_profile,
    subtest_bar_chart,
)
from demat.reports.child_data import ChildReportData
from demat.reports.data import BucketMember, ChildData, ClassProfileData
from demat.reports.prose import ClassProse

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Spec §8.2: the printable A4-portrait area at 96 dpi with 12 mm margins,
# and the per-section height budget the worst-case fixture is checked
# against (test ceiling: 95 % of this, spec §8.2).
PAGE_BUDGET_PX = 1032
SECTION_BUDGET_PX = {
    "h1": 32,
    "header_table": 60,
    "rating_block": 60,
    "rating_band_table": 62,
    "h2_ergebnisse": 26,
    "sheet_svg_gap": 8,
    "subtest_paragraph": 38,
    "h2_hinweise": 26,
    "bullet": 25,
    "notes_1_line": 20,
    "notes_2_line": 42,
    "notes_gap": 8,
    "footer": 40,
}


def _sheet_svg_height(data: ChildReportData) -> int:
    """The rendered sheet SVG's `height` attribute (spec §7.1/§7.2's
    formulas), read back from the actual markup rather than re-derived, so
    it can never drift from what `_sheet_svg()` produces.
    """
    svg = _sheet_svg(data)
    match = re.search(r'height="(\d+)"', svg)
    if not match:
        raise RuntimeError("sheet SVG has no height attribute")
    return int(match.group(1))


def estimate_height_px(data: ChildReportData) -> int:
    """Spec §8.2's page-height estimate: every section is fixed-height or
    capped, so the whole page bounds without measuring an actual render.
    """
    b = SECTION_BUDGET_PX
    total = (
        b["h1"]
        + b["header_table"]
        + b["rating_block"]
        + b["rating_band_table"]
        + b["h2_ergebnisse"]
        + _sheet_svg_height(data)
        + b["sheet_svg_gap"]
        + 2 * b["subtest_paragraph"]
        + b["h2_hinweise"]
        + len(data.hint_lines) * b["bullet"]
        + (b["notes_1_line"] if data.notes_lines == 1 else b["notes_2_line"])
        + b["notes_gap"]
        + b["footer"]
    )
    return total


def md_cell(value: object) -> str:
    """Jinja filter (spec §4): HTML-escape and collapse whitespace. Every
    free-text value in the child-report template passes through this —
    the template's `autoescape` is off (same setting as the class-profile
    environment), so escaping is explicit and applies uniformly whether
    the value came from a handwritten field or a static string.
    """
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return _xml_escape(text)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md_cell"] = md_cell
    return env


def _de_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _sheet_svg(data: ChildReportData) -> str:
    if data.sheet_layout == "columns_with_corridor":
        return sheet_columns_with_corridor(data)
    return sheet_rows_with_profile(data)


def _rating_line(data: ChildReportData) -> str:
    basis_word = "Prozentrang" if data.classification_basis == "pr" else "T-Wert"
    if data.overall_value is None:
        category_text = "nicht bestimmbar (kein Normwert erfasst)"
    elif data.child.overall_categories and len(data.child.overall_categories) > 1:
        cats = " / ".join(data.child.overall_categories)
        category_text = (
            f"{cats} (Grenzwert; das Manual druckt hier überlappende Bereiche)"
        )
    else:
        category_text = data.child.overall_category or "nicht bestimmbar"
    return (
        f"**Gesamteinordnung: {category_text}** (Beurteilung nach {basis_word}; "
        f"{data.classification_source})"
    )


def _highlighted_categories(data: ChildReportData) -> set[str]:
    if data.overall_value is None:
        return set()
    if data.child.overall_categories:
        return set(data.child.overall_categories)
    if data.child.overall_category:
        return {data.child.overall_category}
    return set()


def _footer(data: ChildReportData) -> str:
    id_part = (
        f"ID {data.person_id}"
        if data.person_id
        else f"ID nicht verknüpft (Bogen #{data.assessment_id})"
    )
    parts = [id_part, f"Bogen #{data.assessment_id}"]
    version_form = data.test_version
    if data.test_form:
        version_form += f" Form {data.test_form}"
    parts.append(version_form)
    parts.append(f"geprüft {_de_date(data.finalized_at)}")
    parts.append(f"erstellt {_de_date(data.generated_at)}")
    if data.provenance_phrase:
        parts.append(data.provenance_phrase)
    parts.append(f"demat-plus Einzelauswertung v{data.template_version}")
    return " · ".join(parts)


def render_child_markdown(data: ChildReportData) -> str:
    """Stage 4 (spec §3/§5): assemble the one-page markdown+inline-SVG
    artifact for one validated assessment. Deterministic; no LLM, no KB
    call, no Streamlit.
    """
    env = _env()
    template = env.get_template("child_report.md.j2")

    sheet_svg = _sheet_svg(data)

    write_in_blank = "_" * 22
    header_name = data.display_name if data.display_name else write_in_blank

    test_label = data.test_version
    if data.test_form:
        test_label += f", Form {data.test_form}"

    context = {
        "data": data,
        "header_name": header_name,
        "testdatum": _de_date(data.test_date_iso),
        "test_label": test_label,
        "normtabelle_label": data.norm_variant_label or "nicht erfasst",
        "rating_line": _rating_line(data),
        "highlighted_categories": _highlighted_categories(data),
        "basis_prefix": "PR " if data.classification_basis == "pr" else "T ",
        "sheet_svg": sheet_svg,
        "t_basis_caption": (
            f"{data.test_version} beurteilt nur den Gesamtwert (nach T-Wert). "
            "Für die Teilbereiche gibt es nur Prozentränge und keine "
            "Einordnung laut Manual."
            if data.classification_basis == "t"
            else None
        ),
        "notes_svg": notes_area(data.notes_lines),
        "footer": _footer(data),
    }
    return template.render(**context)


def _abbreviate_members(members: list[BucketMember]) -> str:
    """Spec §9: list up to 4 members, then "<first> bis <last> (N Kinder)"."""
    labels = [m.display_label for m in members]
    if len(labels) <= 4:
        return ", ".join(labels)
    return f"{labels[0]} bis {labels[-1]} ({len(labels)} Kinder)"


def _distribution_context(data: ClassProfileData) -> list[dict]:
    n_categories = len(data.categories_ordered)
    rows = []
    for bucket in data.distribution:
        rows.append(
            {
                "category": bucket.category,
                "count": bucket.count,
                "color": category_color(bucket.ordinal_rank, n_categories),
                "members_label": _abbreviate_members(bucket.members)
                if bucket.members
                else "—",
            }
        )
    return rows


def _subtest_chart(data: ClassProfileData) -> str:
    n_categories = len(data.categories_ordered)
    rows = []
    for row in data.subtest_rows:
        color = None
        if row.category and row.category in data.categories_ordered:
            color = category_color(
                data.categories_ordered.index(row.category), n_categories
            )
        rows.append(
            {
                "name": row.name,
                "printed_label": row.printed_label,
                "mean_pr": row.mean_pr,
                "n_present": row.n_present,
                "n_children": row.n_children,
                "color": color,
            }
        )
    return subtest_bar_chart(rows)


def _child_sparkline_svg(data: ClassProfileData, child: ChildData) -> str:
    n_categories = len(data.categories_ordered)
    series = []
    for row in data.subtest_rows:
        pr = child.subtest_pr.get(row.key)
        color = None
        cat = child.subtest_category.get(row.key)
        if cat and cat in data.categories_ordered:
            color = category_color(data.categories_ordered.index(cat), n_categories)
        series.append({"key": row.key, "pr": pr, "color": color})
    return child_sparkline(series)


def _sorted_children(data: ClassProfileData) -> list[ChildData]:
    return sorted(
        data.children,
        key=lambda c: (c.overall_value is None, -(c.overall_value or 0)),
    )


def render_markdown(data: ClassProfileData, prose: ClassProse) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("class_profile.md.j2")

    prose_by_ref = {c.person_ref: c for c in prose.kinder}
    children_ctx = []
    for child in _sorted_children(data):
        child_prose = prose_by_ref.get(child.person_ref)
        children_ctx.append(
            {
                "display_label": child.display_label,
                "overall_value": child.overall_value,
                "overall_category": child.overall_category,
                "overall_categories": child.overall_categories,
                "classification_note": child.classification_note,
                "sparkline_svg": _child_sparkline_svg(data, child),
                "staerken": child_prose.staerken if child_prose else "",
                "schwaechen": child_prose.schwaechen if child_prose else "",
                "empfehlungen": child_prose.empfehlungen if child_prose else [],
            }
        )

    if (
        data.test_date_min
        and data.test_date_max
        and data.test_date_min == data.test_date_max
    ):
        testdatum_label = f"Testdatum: {data.test_date_min}"
    elif data.test_date_min and data.test_date_max:
        testdatum_label = f"Testzeitraum: {data.test_date_min} – {data.test_date_max}"
    else:
        testdatum_label = "Testdatum: —"

    context = {
        "data": data,
        "prose": prose,
        "children": children_ctx,
        "distribution": _distribution_context(data),
        "subtest_chart_svg": _subtest_chart(data),
        "testdatum_label": testdatum_label,
        "awaiting_review_line": (
            f"Stand: {data.awaiting_review_count} von "
            f"{data.child_count + data.awaiting_review_count} Bögen dieser Klasse "
            "sind noch nicht geprüft."
            if data.awaiting_review_count > 0
            else None
        ),
    }
    return template.render(**context)
