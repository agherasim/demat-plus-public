"""Stage 2 — child-report data assembly (ADR 0014 spec §6.4). Pure
functions over already-validated `store` rows; no Streamlit, no LLM, no KB
subprocess call (those are injected via `classify_fn`/`bands`/
`median_zone`, all computed by `demat/reports/norms.py`). Every number on
the page is computed here or is static template text.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

from demat.reports.data import (
    ChildData,
    _person_ref,
    build_child_data,
    overall_metric_key,
)
from demat.reports.norms import CONFIDENCE_LEVEL, BandSegment, ClassifyFn
from demat.test_registry import TestDefinition
from demat.validation import coerce_float

ELIGIBLE_STATUSES = {"validated_ok", "validated_with_changes"}

_NAME_MAX = 34
_WRITE_IN_BLANK = "_" * 22


class SheetSubtest(BaseModel):
    key: str
    name: str  # truncated to 34 chars with "…"
    column_title: str  # 3+ column header (printed_label)
    rw_display: str  # "2 (2)" / "7 (15)" / "— (4)"
    pr: float | None  # plotted value; None -> no square
    pr_display: str  # "81", "—", "≤1" (verbatim), "n. n." (unnormed)
    pr_is_value: bool  # True -> white cell
    normed: bool
    category: str | None  # PR basis only, for §6.5 lines; never rendered as colour


class GesamtField(BaseModel):
    label: str
    display: str
    kind: Literal["value", "range"]  # value -> white cell; range -> grey cell
    present: bool


class ChildReportData(BaseModel):
    child: ChildData
    assessment_id: int
    person_id: str | None
    person_ref: str
    display_name: str | None
    klasse: str | None
    test_version: str
    test_key: str
    norm_test: str
    test_form: str | None
    test_date_iso: str | None
    norm_variant: str | None
    norm_variant_label: str | None
    classification_basis: Literal["pr", "t"]
    classification_source: str
    confidence_level: str
    sheet_layout: Literal["rows_with_profile", "columns_with_corridor"]
    subtests: list[SheetSubtest]
    rw_total_display: str
    overall_value: float | None
    overall_band_range: tuple[float, float] | None
    median_zone: tuple[float, float] | None
    gesamt_fields: list[GesamtField]
    rating_categories: list[str]
    rating_rank: int | None
    bands: tuple[BandSegment, ...]
    profile_mode: Literal["categorised", "relative"]
    profile_high: str
    profile_low: str
    hint_lines: list[str]
    notes_lines: Literal[1, 2]
    provenance_phrase: str | None
    finalized_at: str | None
    generated_at: str
    template_version: str


# Spec §6.4's table: per-`norm_test` sheet layout and Gesamtwert field
# order, driven by data (not a forked template — spec §2/§8).
SHEET_LAYOUT: dict[str, dict[str, Any]] = {
    "demat1plus": {
        "layout": "rows_with_profile",
        "gesamt_fields": [
            ("pr_total", "Prozentrang (ges)", "value"),
            ("t_total", "T-Wert (ges)", "value"),
            ("t_band", "T-Wert-Band", "range"),
        ],
        # DEVIATION (judgment call): the two worked examples print a
        # different PR sub-label per version ("(Handbuch)" on 2+ vs
        # "(Normtab.)" on 1+); the spec gives no rule for which, so this is
        # a per-version display constant rather than a formula.
        "pr_source_label": "(Normtab.)",
    },
    "demat2plus": {
        "layout": "rows_with_profile",
        "gesamt_fields": [
            ("pr_total", "Prozentrang (ges)", "value"),
            ("pr_band", "Prozentrang-Band", "range"),
            ("t_total", "T-Wert (ges)", "value"),
            ("t_band", "T-Wert-Band", "range"),
        ],
        "pr_source_label": "(Handbuch)",
    },
    "demat3plus": {
        # The corridor draws bands as dots, not a Gesamtwert block, but the
        # total's Prozentrang-Band still needs a display string somewhere
        # in the fixed contract (spec §6.4/R-5: an unparseable band shows
        # verbatim); this single entry supplies it to
        # `sheet_columns_with_corridor` without adding a new field.
        "layout": "columns_with_corridor",
        "gesamt_fields": [("pr_band", "Prozentrang-Band", "range")],
    },
    "demat5plus": {
        "layout": "rows_with_profile",
        "gesamt_fields": [
            ("pr_total", "Prozentrang (ges)", "value"),
            ("t_total", "T-Wert (ges)", "value"),
            ("t_band", "T-Wert-Band", "range"),
        ],
        "pr_source_label": "(Normtab.)",
    },
}

_BAND_RANGE_RE = re.compile(
    r"^\s*(?P<lo>-?\d+(?:[.,]\d+)?)\s*[-–]\s*(?P<hi>-?\d+(?:[.,]\d+)?)\s*$"
)


def _parse_band_range(raw: str | None) -> tuple[float, float] | None:
    """Parse a stored `pr_band` string into `(lo, hi)` (spec §6.4). Accepts
    `lo-hi` or `lo–hi`, and a decimal comma. `None` on any other shape —
    the caller then shows the text verbatim and draws no dots (spec R-5).
    """
    if not raw:
        return None
    match = _BAND_RANGE_RE.match(raw)
    if not match:
        return None
    try:
        lo = float(match.group("lo").replace(",", "."))
        hi = float(match.group("hi").replace(",", "."))
    except ValueError:
        return None
    return (lo, hi)


def _truncate_name(name: str, limit: int = _NAME_MAX) -> str:
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


def _metric_display(metrics_by_key: dict[str, dict[str, Any]], key: str) -> str | None:
    m = metrics_by_key.get(key)
    if m is None:
        return None
    if m.get("value_text") is not None:
        return str(m["value_text"])
    if m.get("value_raw") is not None:
        return str(m["value_raw"])
    if m.get("value_num") is not None:
        value = m["value_num"]
        return f"{value:g}" if isinstance(value, int | float) else str(value)
    return None


def _metric_numeric(
    metrics_by_key: dict[str, dict[str, Any]], key: str
) -> float | None:
    m = metrics_by_key.get(key)
    if m is None:
        return None
    if m.get("value_num") is not None:
        return float(m["value_num"])
    return coerce_float(m.get("value_raw"))


def _format_rw(rw: float | None, rw_max: float | None) -> str:
    rw_str = "—" if rw is None else f"{rw:g}"
    max_str = "?" if rw_max is None else f"{rw_max:g}"
    return f"{rw_str} ({max_str})"


def _subtest_pr_display(
    value_raw: str | None, pr: int | float | None, normed: bool
) -> tuple[str, bool]:
    """(`pr_display`, `pr_is_value`) per spec §6.4's examples: an unnormed
    subtest shows the fixed "n. n." (grey, no square); a present PR shows
    the number (white cell); a literal the sheet printed but that could not
    be normed (e.g. "≤1") is shown verbatim, with no square; nothing
    printed shows "—".
    """
    if not normed:
        return "n. n.", False
    if pr is not None:
        return f"{pr:g}" if isinstance(pr, int | float) else str(pr), True
    if value_raw:
        return value_raw, False
    return "—", False


def _hint_lines(
    *,
    classification_basis: str,
    confidence_level: str,
    rating_rank: int | None,
    n_categories: int,
    gesamt_caveat: str | None,
    norm_variant_label: str | None,
    cross_check_warning: str | None,
) -> list[str]:
    """3 static bullets, then up to 3 conditional ones in a fixed order
    (spec §5 row 10, §6.6; wording reconstructed from the worked examples
    A/B/C — see the implementation report's DEVIATIONS for the exact
    provenance of each string).
    """
    band_label = "PR-Band" if classification_basis == "pr" else "T-Wert-Band"
    lines = [
        "Maßgeblich ist der Gesamtwert. Teilbereiche umfassen nur wenige "
        "Aufgaben und zeigen nur Tendenzen.",
        f"Das {band_label} umfasst den „wahren“ Wert mit etwa "
        f"{confidence_level} Wahrscheinlichkeit.",
        "Screening-Ergebnis: Ein DEMAT-Ergebnis allein begründet keine "
        "Diagnose (Rechenstörung, Dyskalkulie).",
    ]
    conditional: list[str] = []
    # Below-average recommendation: rating_rank is 0-indexed worst -> best;
    # "below average" means strictly below the middle category.
    if rating_rank is not None and rating_rank < (n_categories - 1) / 2:
        conditional.append(
            "Unter dem Durchschnitt empfehlen die Manuale eine genauere "
            "Diagnostik (z. B. Basiskompetenzen) und zeitnahe Förderung."
        )
    if gesamt_caveat:
        conditional.append(gesamt_caveat)
    if norm_variant_label is None:
        conditional.append(
            "Hinweis: Für dieses Ergebnis ist keine Normtabelle erfasst; "
            "die Eingruppierung nennt daher keine Vergleichsnorm."
        )
    elif cross_check_warning:
        conditional.append(cross_check_warning)
    return lines + conditional[:3]


MEDIAN_RANK_CATEGORY = "durchschnittlich"


def _profile_lines(
    *,
    classification_basis: str,
    subtests: list[SheetSubtest],
    categories_ordered: list[str],
) -> tuple[Literal["categorised", "relative"], str, str]:
    """The two subtest paragraphs (spec §6.5): PR basis lists subtests
    above/below the manual's "durchschnittlich" category (`profile_mode`
    "categorised"); T basis has no per-subtest category and instead ranks
    the child's own present PRs ("relative"). Each list is capped at 4
    entries (spec §6).
    """
    present = [s for s in subtests if s.pr is not None]

    def _pr_key(s: SheetSubtest) -> float:
        assert s.pr is not None  # `present` is already filtered
        return s.pr

    if classification_basis == "pr" and MEDIAN_RANK_CATEGORY in categories_ordered:
        median_rank = categories_ordered.index(MEDIAN_RANK_CATEGORY)
        above = sorted(
            (
                s
                for s in present
                if s.category
                and s.category in categories_ordered
                and categories_ordered.index(s.category) > median_rank
            ),
            key=_pr_key,
            reverse=True,
        )[:4]
        below = sorted(
            (
                s
                for s in present
                if s.category
                and s.category in categories_ordered
                and categories_ordered.index(s.category) < median_rank
            ),
            key=_pr_key,
        )[:4]

        def _fmt_above(s: SheetSubtest) -> str:
            return f"{s.name} (PR {s.pr:g})"

        def _fmt_below(s: SheetSubtest) -> str:
            return f"{s.name} (PR {s.pr:g}, {s.category})"

        high_text = ", ".join(_fmt_above(s) for s in above) if above else "—"
        low_text = ", ".join(_fmt_below(s) for s in below) if below else "—"
        return (
            "categorised",
            f"**Über dem Durchschnittsbereich:** {high_text}",
            f"**Unter dem Durchschnittsbereich:** {low_text}",
        )

    ranked_desc = sorted(present, key=_pr_key, reverse=True)[:4]
    ranked_asc = sorted(present, key=_pr_key)[:4]
    high_text = (
        ", ".join(f"{s.name} (PR {s.pr:g})" for s in ranked_desc)
        if ranked_desc
        else "—"
    )
    low_text = (
        ", ".join(f"{s.name} (PR {s.pr:g})" for s in ranked_asc) if ranked_asc else "—"
    )
    return (
        "relative",
        "**Relativ höchste Teilbereiche (innerhalb des eigenen Profils):** "
        f"{high_text}",
        "**Relativ niedrigste Teilbereiche (innerhalb des eigenen Profils):** "
        f"{low_text}",
    )


def build_child_report_data(
    *,
    assessment: dict[str, Any],
    metrics: list[dict[str, Any]],
    subtests: list[dict[str, Any]],
    definition: TestDefinition,
    store_raw_names: bool,
    classification_basis: Literal["pr", "t"],
    classification_source: str,
    classify_fn: ClassifyFn,
    bands: tuple[BandSegment, ...],
    categories_ordered: list[str],
    median_zone: tuple[float, float] | None,
    norm_variant_label: str | None,
    cross_check_warning: str | None,
    gesamt_status: str | None,
    provenance_phrase: str | None,
    generated_at: str,
    template_version: str,
) -> ChildReportData:
    """Assemble the complete `ChildReportData` for one validated assessment
    (spec §6.4). `classify_fn`/`bands`/`median_zone` are injected — this
    module never calls the KB. Raises `ValueError` when the assessment is
    not eligible (spec §4) or the definition has no `norm_test`.
    """
    status = assessment.get("status")
    if status not in ELIGIBLE_STATUSES:
        raise ValueError(
            f"assessment {assessment.get('assessment_id')}: status {status!r} "
            f"is not eligible for a child report (need one of {ELIGIBLE_STATUSES})"
        )
    if definition.norm_test is None:
        raise ValueError(f"{definition.test_version}: no norm_test")
    norm_test = definition.norm_test

    assessment_id = assessment["assessment_id"]
    person_id = assessment.get("person_id")
    person_ref = _person_ref(person_id, assessment_id)
    raw_name = assessment.get("person_name") if store_raw_names else None
    display_name = raw_name if (store_raw_names and raw_name) else None

    # Aufgabentyp rows (DEMAT 3+, ADR 0015) only exist to be summed into a
    # subtest and carry no PR — keep them out of every subtest aggregate and
    # off the printed sheet reproduction below. Empty for 1+/2+/5+.
    component_keys = definition.component_keys()

    overall_key = overall_metric_key(definition, classification_basis)
    child = build_child_data(
        assessment_id=assessment_id,
        person_id=person_id,
        raw_name=raw_name,
        store_raw_names=store_raw_names,
        overall_key=overall_key,
        metrics=metrics,
        subtests=subtests,
        classification_basis=classification_basis,
        categories_ordered=categories_ordered,
        classify_fn=classify_fn,
        component_keys=component_keys,
    )

    rating_categories = list(categories_ordered)
    rating_rank: int | None = None
    if child.overall_category and child.overall_category in rating_categories:
        rating_rank = rating_categories.index(child.overall_category)
    elif child.overall_categories:
        ranked = [c for c in rating_categories if c in child.overall_categories]
        if ranked:
            rating_rank = rating_categories.index(ranked[0])

    metrics_by_key = {m["metric_key"]: m for m in metrics}
    sheet_layout: Literal["rows_with_profile", "columns_with_corridor"] = SHEET_LAYOUT[
        norm_test
    ]["layout"]

    confidence_level = CONFIDENCE_LEVEL.get(norm_test, "68 %")

    # --- Subtests ------------------------------------------------------
    subtest_by_key = {s["subtest_key"]: s for s in subtests}
    sheet_subtests: list[SheetSubtest] = []
    for s in definition.subtests:
        if s.key in component_keys:
            continue
        row = subtest_by_key.get(s.key, {})
        rw = row.get("rw")
        pr = row.get("pr")
        normed = s.norm_scale is not None
        pr_display, pr_is_value = _subtest_pr_display(row.get("value_raw"), pr, normed)
        category = None
        if classification_basis == "pr" and pr is not None:
            category = classify_fn(pr).get("category")
        sheet_subtests.append(
            SheetSubtest(
                key=s.key,
                name=_truncate_name(s.name),
                column_title=s.printed_label or s.name,
                rw_display=_format_rw(rw, s.rw_max),
                pr=float(pr) if pr is not None else None,
                pr_display=pr_display,
                pr_is_value=pr_is_value,
                normed=normed,
                category=category,
            )
        )

    # --- Gesamtwert fields / rw_total -----------------------------------
    rw_total = _metric_numeric(metrics_by_key, "rw_total")
    rw_total_max = next(
        (m.max_possible for m in definition.overall_metrics if m.key == "rw_total"),
        None,
    )
    rw_total_display = _format_rw(rw_total, rw_total_max)

    gesamt_fields: list[GesamtField] = []
    for metric_key, label, kind in SHEET_LAYOUT[norm_test]["gesamt_fields"]:
        display = _metric_display(metrics_by_key, metric_key)
        full_label = f"{label} ({confidence_level})" if kind == "range" else label
        gesamt_fields.append(
            GesamtField(
                label=full_label,
                display=display if display is not None else "—",
                kind=kind,
                present=display is not None,
            )
        )

    # --- Overall band range (3+ corridor only, spec §6.4) ---------------
    overall_band_range: tuple[float, float] | None = None
    if sheet_layout == "columns_with_corridor":
        overall_band_range = _parse_band_range(
            _metric_display(metrics_by_key, "pr_band")
        )

    # --- Subtest commentary (spec §6.5) ---------------------------------
    profile_mode, profile_high, profile_low = _profile_lines(
        classification_basis=classification_basis,
        subtests=sheet_subtests,
        categories_ordered=categories_ordered,
    )

    gesamt_caveat = None
    if gesamt_status is not None:
        from demat.reports.norms import gesamt_status_caveat

        gesamt_caveat = gesamt_status_caveat(gesamt_status, rw=rw_total)

    hint_lines = _hint_lines(
        classification_basis=classification_basis,
        confidence_level=confidence_level,
        rating_rank=rating_rank,
        n_categories=len(rating_categories),
        gesamt_caveat=gesamt_caveat,
        norm_variant_label=norm_variant_label,
        cross_check_warning=cross_check_warning,
    )

    return ChildReportData(
        child=child,
        assessment_id=assessment_id,
        person_id=person_id,
        person_ref=person_ref,
        display_name=display_name,
        klasse=assessment.get("klasse"),
        test_version=assessment.get("test_version") or definition.test_version,
        test_key=definition.test_key,
        norm_test=norm_test,
        test_form=assessment.get("test_form"),
        test_date_iso=assessment.get("test_date_iso"),
        norm_variant=assessment.get("norm_variant"),
        norm_variant_label=norm_variant_label,
        classification_basis=classification_basis,
        classification_source=classification_source,
        confidence_level=confidence_level,
        sheet_layout=sheet_layout,
        subtests=sheet_subtests,
        rw_total_display=rw_total_display,
        overall_value=child.overall_value,
        overall_band_range=overall_band_range,
        median_zone=median_zone,
        gesamt_fields=gesamt_fields,
        rating_categories=rating_categories,
        rating_rank=rating_rank,
        bands=bands,
        profile_mode=profile_mode,
        profile_high=profile_high,
        profile_low=profile_low,
        hint_lines=hint_lines,
        # Spec §7.3: every sheet SVG except the provisional DEMAT 5+ one
        # (196 px) is taller than 300 px, so gets 1 ruled line; 5+ gets 2.
        # A version property, decided here rather than re-measured from
        # rendered markup at render time.
        notes_lines=2 if norm_test == "demat5plus" else 1,
        provenance_phrase=provenance_phrase,
        finalized_at=assessment.get("finalized_at"),
        generated_at=generated_at,
        template_version=template_version,
    )


__all__ = [
    "ELIGIBLE_STATUSES",
    "SHEET_LAYOUT",
    "ChildReportData",
    "GesamtField",
    "SheetSubtest",
    "build_child_report_data",
]
