"""Classroom Profile report generation (ADR 0008). Public entry point:
`generate_class_profile()` — the five stages of spec §3, orchestrated.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from demat import config, store
from demat.agent_runner import SkillRunner
from demat.class_groups import ClassGroup, content_hash
from demat.reports import norms
from demat.reports.data import ClassProfileData, build_class_data
from demat.reports.prose import ClassProse
from demat.reports.prose import generate as generate_prose
from demat.reports.render import render_markdown

ProgressCallback = Callable[[int, int, str], None]

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class ClassProfileResult:
    profile_id: int
    data: ClassProfileData
    prose: ClassProse
    markdown: str
    markdown_path: str


def _slugify(schuljahr: str, klasse: str, test_key: str) -> str:
    raw = f"{schuljahr}_{klasse}_{test_key}"
    return _SLUG_UNSAFE.sub("-", raw)


def generate_class_profile(
    group: ClassGroup,
    runner: SkillRunner,
    progress_cb: ProgressCallback | None = None,
) -> ClassProfileResult:
    """Run the five stages of spec §3 for one class group and persist the
    result (`data/reports/` + a `class_profiles` row). Raises
    `norms.KnowledgeBaseUnavailableError` if the knowledge base is absent
    — a normal state, never a crash (ADR 0005 §9.6).
    """
    total_stages = 5

    def _progress(i: int, label: str) -> None:
        if progress_cb:
            progress_cb(i, total_stages, label)

    _progress(1, "Lade Klassendaten")
    if not norms.kb_available():
        raise norms.KnowledgeBaseUnavailableError(
            f"Knowledge base not found at {config.KNOWLEDGE_BASE_DIR}. "
            "Class Profile generation is disabled until it is present."
        )

    definition = store.get_test_definition(group.test_version)
    if definition is None or definition.norm_test is None:
        raise ValueError(
            f"no test definition (with norm_test) for {group.test_version!r}"
        )
    norm_test: str = definition.norm_test

    assessment_rows = store.get_class_assessments(
        group.klasse, group.schuljahr, group.test_version
    )
    metrics_by_assessment = {
        row["assessment_id"]: store.get_assessment_metrics(row["assessment_id"])
        for row in assessment_rows
    }
    subtests_by_assessment = {
        row["assessment_id"]: store.get_subtest_scores(row["assessment_id"])
        for row in assessment_rows
    }
    raw_names_by_assessment: dict[int, str | None] = {}
    if config.STORE_RAW_NAMES:
        for row in assessment_rows:
            full = store.get_assessment(row["assessment_id"])
            raw_names_by_assessment[row["assessment_id"]] = (
                full.get("person_name") if full else None
            )

    _progress(2, "Bestimme Normgruppe")
    basis = norms.basis_for(definition)
    if len(group.norm_variants) > 1:
        raise ValueError(
            "Diese Klasse enthält Bögen mit unterschiedlichen Normtabellen "
            f"({', '.join(group.norm_variants)}). Ein Klassenprofil vergleicht nur "
            "Werte aus derselben Normtabelle — bitte die Normtabelle in der Prüfung "
            "angleichen."
        )
    norm_group: str | None
    norm_group_label: str | None
    cross_check_warning: str | None
    if group.norm_variant is None:
        # All-NULL: generate, unlabelled (ADR 0010) — nothing recorded, not
        # a guess.
        norm_group = norm_group_label = None
        cross_check_warning = (
            "Hinweis: Für diese Klasse ist keine Normtabelle erfasst; der "
            "Bericht nennt daher keine Vergleichsnorm."
        )
    else:
        norm_group, norm_group_label, _variant = norms.resolve_norm_group(
            norm_test, group.norm_variant
        )
        cross_check_warning = norms.grade_cross_check_warning(
            group.klasse, norm_test, group.norm_variant, norm_group
        )

    classify_fn = norms.classifier(norm_test, basis)

    _progress(3, "Berechne Verteilung")
    class_norm_result = None
    if norm_test == "demat3plus":
        rw_totals = []
        for metrics in metrics_by_assessment.values():
            for m in metrics:
                if m["metric_key"] == "rw_total" and m["value_num"] is not None:
                    rw_totals.append(m["value_num"])
                    break
        if len(rw_totals) >= 2:
            class_norm_result = norms.class_norm("GES", rw_totals)

    data = build_class_data(
        klasse=group.klasse,
        schuljahr=group.schuljahr,
        test_version=group.test_version,
        test_form_summary=group.test_form_summary,
        test_date_min=group.test_date_min,
        test_date_max=group.test_date_max,
        awaiting_review_count=group.awaiting_review_count,
        assessment_rows=assessment_rows,
        metrics_by_assessment=metrics_by_assessment,
        subtests_by_assessment=subtests_by_assessment,
        raw_names_by_assessment=raw_names_by_assessment,
        store_raw_names=config.STORE_RAW_NAMES,
        definition=definition,
        classification_basis=basis,
        classify_fn=classify_fn,
        norm_group=norm_group,
        norm_group_label=norm_group_label,
        norm_variant=group.norm_variant,
        cross_check_warning=cross_check_warning,
        class_norm=class_norm_result,
    )

    _progress(4, "Erzeuge Text (LLM)")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = config.RUNS_DIR / f"class-profile-{timestamp}"
    prose = generate_prose(data, runner, run_dir)

    _progress(5, "Rendere Bericht")
    markdown = render_markdown(data, prose)

    slug = _slugify(group.schuljahr, group.klasse, definition.test_key)
    report_dir = config.REPORTS_DIR / slug
    report_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = report_dir / f"{timestamp}.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    hash_value = content_hash(assessment_rows)
    profile_id = store.save_class_profile(
        klasse=group.klasse,
        schuljahr=group.schuljahr,
        test_version=group.test_version,
        test_form_summary=group.test_form_summary,
        test_date_min=group.test_date_min,
        test_date_max=group.test_date_max,
        norm_group=norm_group,
        classification_basis=basis,
        child_count=data.child_count,
        assessment_ids=group.assessment_ids,
        markdown_path=str(markdown_path),
        content_hash=hash_value,
        prose_run_dir=str(run_dir),
        model_id=getattr(runner, "model_id", None),
        runner=config.RUNNER,
    )

    return ClassProfileResult(
        profile_id=profile_id,
        data=data,
        prose=prose,
        markdown=markdown,
        markdown_path=str(markdown_path),
    )


from demat.reports.child_report import (  # noqa: E402
    ChildReportResult,
    generate_child_report,
    generate_class_child_reports,
)

__all__ = [
    "ChildReportResult",
    "ClassProfileResult",
    "generate_child_report",
    "generate_class_child_reports",
    "generate_class_profile",
]
