"""Child report generation (ADR 0014). Public entry points:
`generate_child_report()` / `generate_class_child_reports()` — the four
stages of spec §3, orchestrated: load, assemble, norms, render.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from demat import config, store
from demat.class_groups import content_hash
from demat.reports import norms
from demat.reports.child_data import ChildReportData, build_child_report_data
from demat.reports.render import render_child_markdown

TEMPLATE_VERSION = "1"

ProgressCallback = Callable[[int, int, str], None]

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

_MANUAL_SOURCE = {
    "demat1plus": "Manual DEMAT 1+, Kap. 7.2, S. 25",
    "demat2plus": "Manual DEMAT 2+, Kap. 7.2, S. 24",
    "demat3plus": "Manual DEMAT 3+, Kap. 7.2, Tabelle 4, S. 25",
    "demat5plus": "Manual DEMAT 5+, Kap. 7.2, Tabelle 3, S. 21",
}


@dataclass
class ChildReportResult:
    report_id: int
    data: ChildReportData
    markdown: str
    markdown_path: str


def _slugify(person_ref: str) -> str:
    return _SLUG_UNSAFE.sub("-", person_ref)


def generate_child_report(assessment_id: int) -> ChildReportResult:
    """Run spec §3's four stages for one validated assessment and persist
    the result (`data/reports/children/<person_ref>/` + a `child_reports`
    row). Raises `ValueError` if the assessment is not eligible (spec §4)
    or has no recognised `test_version`, and
    `norms.KnowledgeBaseUnavailableError` if the knowledge base is absent
    — a normal state, never a crash (ADR 0005 §9.6).
    """
    if not norms.kb_available():
        raise norms.KnowledgeBaseUnavailableError(
            f"Knowledge base not found at {config.KNOWLEDGE_BASE_DIR}. "
            "Child report generation is disabled until it is present."
        )

    assessment = store.get_assessment(assessment_id)
    if assessment is None:
        raise ValueError(f"no assessment {assessment_id}")

    definition = store.get_test_definition(assessment.get("test_version"))
    if definition is None or definition.norm_test is None:
        raise ValueError(
            f"no test definition (with norm_test) for "
            f"{assessment.get('test_version')!r}"
        )
    norm_test = definition.norm_test

    metrics = store.get_assessment_metrics(assessment_id)
    subtests = store.get_subtest_scores(assessment_id)

    basis = norms.basis_for(definition)
    classify_fn = norms.classifier(norm_test, basis)

    from demat.reports.norms import CATEGORIES_ORDERED

    categories_ordered = CATEGORIES_ORDERED[norm_test]
    bands = norms.band_segments(norm_test)
    median_zone_value = norms.median_zone(norm_test)

    norm_variant = assessment.get("norm_variant")
    norm_variant_label = norm_variants_label_for(norm_test, norm_variant)
    cross_check_warning = None
    if norm_variant is not None:
        try:
            norm_group, _label, _v = norms.resolve_norm_group(norm_test, norm_variant)
            cross_check_warning = norms.grade_cross_check_warning(
                assessment.get("klasse"), norm_test, norm_variant, norm_group
            )
        except ValueError:
            cross_check_warning = None

    generated_at = datetime.now().strftime("%Y-%m-%d")

    data = build_child_report_data(
        assessment=assessment,
        metrics=metrics,
        subtests=subtests,
        definition=definition,
        store_raw_names=config.STORE_RAW_NAMES,
        classification_basis=basis,  # type: ignore[arg-type]
        classification_source=_MANUAL_SOURCE.get(norm_test, "Manual"),
        classify_fn=classify_fn,
        bands=bands,
        categories_ordered=categories_ordered,
        median_zone=median_zone_value,
        norm_variant_label=norm_variant_label,
        cross_check_warning=cross_check_warning,
        # ADR 0013's per-cell provenance columns are not in `schema.sql`
        # yet (spec §12) — there is no KB row status to pass through.
        gesamt_status=None,
        provenance_phrase=None,
        generated_at=generated_at,
        template_version=TEMPLATE_VERSION,
    )

    markdown = render_child_markdown(data)

    slug = _slugify(data.person_ref)
    report_dir = config.REPORTS_DIR / "children" / slug
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    markdown_path = report_dir / f"{timestamp}.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    hash_value = content_hash([assessment])
    report_id = store.save_child_report(
        assessment_id=assessment_id,
        person_ref=data.person_ref,
        test_version=data.test_version,
        markdown_path=str(markdown_path),
        template_version=TEMPLATE_VERSION,
        content_hash=hash_value,
    )

    return ChildReportResult(
        report_id=report_id,
        data=data,
        markdown=markdown,
        markdown_path=str(markdown_path),
    )


def norm_variants_label_for(norm_test: str, norm_variant: str | None) -> str | None:
    from demat import norm_variants

    return norm_variants.label_for(norm_test, norm_variant)


def generate_class_child_reports(
    group: object, progress_cb: ProgressCallback | None = None
) -> list[ChildReportResult | Exception]:
    """Generate a child report for every validated assessment in `group`
    (a `class_groups.ClassGroup`). Spec OQ-8: a per-child failure is
    collected, not raised — the caller shows one summary, the rest of the
    class still gets its reports.
    """
    assessment_ids: list[int] = getattr(group, "assessment_ids", [])
    results: list[ChildReportResult | Exception] = []
    total = len(assessment_ids)
    for i, assessment_id in enumerate(assessment_ids, start=1):
        if progress_cb:
            progress_cb(i, total, f"Bericht {i}/{total}")
        try:
            results.append(generate_child_report(assessment_id))
        except Exception as exc:
            results.append(exc)
    return results


__all__ = [
    "TEMPLATE_VERSION",
    "ChildReportResult",
    "generate_child_report",
    "generate_class_child_reports",
]
