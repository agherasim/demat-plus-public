"""Deterministic validation rules, on top of model self-reported confidence.

See ADR 0005 §6. Never auto-corrects a value (ADR 0003); only flags and
writes a human-resolvable detail to `review_notes`. `validate()` takes the
`TestDefinition` (or `None` for the generic-fallback path) instead of an
abbreviation -> row dict; definition-driven checks are skipped when
`definition is None`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from demat.schema import ExtractedSheet, TriageResult
from demat.test_registry import TestDefinition, canonical_subtest_key

# Flags that are informational only and do not force review.
# The store-side identity flags `id_collision`, `possible_person_merge`,
# `duplicate_suspected` and `reextract_name_mismatch` (ADR 0006/0016) are
# intentionally absent here and from `_INFO_FLAGS`: they are always blocking.
_WARNING_ONLY_FLAGS = {"medium_confidence", "unknown_subtest", "unknown_metric"}

# Flags that record a normal, expected step (ADR 0013) — never a warning,
# never blocking. `norm_values_computed`: at least one norm cell was filled
# by `demat/norm_completion.py` from the knowledge base.
_INFO_FLAGS = {"norm_values_computed"}

_MIN_PLAUSIBLE_DATE = date(2000, 1, 1)
_SUM_TOLERANCE = 0.01


def flag_severity(flag: str) -> Literal["error", "warning", "info"]:
    """'info' for a flag in `_INFO_FLAGS`, 'warning' for one in
    `_WARNING_ONLY_FLAGS`, 'error' otherwise — the same split
    `has_blocking_flags`/`is_blocking_flag` use, exposed for UI grouping
    (Assessment Review's Status section groups these alongside
    `review_validation` findings, errors first).
    """
    if flag in _INFO_FLAGS:
        return "info"
    return "warning" if flag in _WARNING_ONLY_FLAGS else "error"


def is_blocking_flag(flag: str) -> bool:
    """True only for an 'error'-severity flag. `has_blocking_flags` ignores
    both warning and info flags.
    """
    return flag_severity(flag) == "error"


@dataclass
class ValidationResult:
    flags: list[str]
    has_blocking_flags: bool
    review_notes: str | None


def _max_plausible_date(today: date) -> date:
    try:
        return today.replace(year=today.year + 1)
    except ValueError:
        # Feb 29 -> no Feb 29 next year.
        return today.replace(month=2, day=28, year=today.year + 1)


def coerce_float(raw: str | None) -> float | None:
    """Accept the German decimal comma; return None if unparseable/blank."""
    if raw is None:
        return None
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _sum_rule_mismatches(
    extracted: ExtractedSheet,
    definition: TestDefinition,
    subtest_rw_values: dict[str, float],
    metric_num_values: dict[str, float],
) -> list[str]:
    """One `total_mismatch: ...` note per `[[rules.sum]]` rule whose printed
    target disagrees with the sum of its parts."""
    notes: list[str] = []

    # List-form rules: canonical key -> parsed RW, skipping keys emitted more
    # than once (ambiguous — `duplicate_subtest` already reports them).
    canonical_counts: Counter[str] = Counter()
    rw_by_canonical: dict[str, float] = {}
    for st in extracted.subtests:
        canonical = canonical_subtest_key(definition, st.key) or st.key
        canonical_counts[canonical] += 1
        rw = coerce_float(st.rw)
        if rw is not None:
            rw_by_canonical[canonical] = rw
    for k, c in canonical_counts.items():
        if c > 1:
            rw_by_canonical.pop(k, None)

    subtest_rules = {r.subtest: r for r in definition.sum_rules_subtest()}

    def component_sum(keys: list[str]) -> float | None:
        if all(k in rw_by_canonical for k in keys):
            return sum(rw_by_canonical[k] for k in keys)
        return None

    for rule in definition.rules_sum:
        if rule.is_legacy:
            assert rule.metric is not None
            total = metric_num_values.get(rule.metric)
            if total is None or not subtest_rw_values:
                continue
            summed = sum(subtest_rw_values.values())
            if abs(summed - total) > _SUM_TOLERANCE:
                notes.append(f"total_mismatch: sum(subtests)={summed} printed={total}")
            continue

        over = definition.over_keys(rule)
        if rule.subtest is not None:
            printed = rw_by_canonical.get(rule.subtest)
            summed_or_none = component_sum(over)
            if printed is None or summed_or_none is None:
                continue
            if abs(summed_or_none - printed) > _SUM_TOLERANCE:
                notes.append(
                    f"total_mismatch: {rule.subtest}: sum({'+'.join(over)})="
                    f"{summed_or_none} printed={printed}"
                )
            continue

        assert rule.metric is not None
        total = metric_num_values.get(rule.metric)
        if total is None:
            continue
        summands: list[float] = []
        for k in over:
            if k in rw_by_canonical:
                summands.append(rw_by_canonical[k])
                continue
            sub_rule = subtest_rules.get(k)
            sub_sum = (
                component_sum(definition.over_keys(sub_rule))
                if sub_rule is not None
                else None
            )
            if sub_sum is None:
                break
            summands.append(sub_sum)
        else:
            summed = sum(summands)
            if abs(summed - total) > _SUM_TOLERANCE:
                notes.append(
                    f"total_mismatch: {rule.metric}: sum({'+'.join(over)})="
                    f"{summed} printed={total}"
                )
    return notes


def validate(
    extracted: ExtractedSheet,
    triage: TriageResult,
    definition: TestDefinition | None,
    *,
    today: date | None = None,
) -> ValidationResult:
    """Validate an extracted sheet against test-agnostic checks plus,
    when `definition` is not None, the definition's declared metrics,
    subtests and `[[rules.sum]]`.
    """
    flags: list[str] = []
    notes: list[str] = []
    today = today or datetime.now().date()

    if extracted.test_version is None:
        flags.append("missing_test_version")

    if (
        triage.test_version
        and extracted.test_version
        and (triage.test_version != extracted.test_version)
    ):
        flags.append("version_mismatch")
        notes.append(
            f"version_mismatch: triage={triage.test_version!r} "
            f"extracted={extracted.test_version!r}"
        )

    if not extracted.child_name:
        flags.append("missing_name")

    low_fields = []
    if extracted.overall_confidence == "low":
        low_fields.append("overall_confidence")
    if extracted.child_name_confidence == "low":
        low_fields.append("child_name_confidence")
    if extracted.test_date_confidence == "low":
        low_fields.append("test_date_confidence")
    for st in extracted.subtests:
        if st.confidence == "low":
            low_fields.append(f"subtest:{st.key}")
    for m in extracted.overall:
        if m.confidence == "low":
            low_fields.append(f"metric:{m.key}")
    if low_fields:
        flags.append("low_confidence")
        notes.append("low_confidence: " + ", ".join(low_fields))

    medium_present = (
        extracted.overall_confidence == "medium"
        or extracted.child_name_confidence == "medium"
        or extracted.test_date_confidence == "medium"
        or any(st.confidence == "medium" for st in extracted.subtests)
        or any(m.confidence == "medium" for m in extracted.overall)
    )
    if medium_present:
        flags.append("medium_confidence")

    key_counts = Counter(st.key for st in extracted.subtests)
    dupes = sorted(k for k, c in key_counts.items() if c > 1)
    if dupes:
        flags.append("duplicate_subtest")
        notes.append("duplicate_subtest: " + ", ".join(dupes))

    metric_counts = Counter(m.key for m in extracted.overall)
    metric_dupes = sorted(k for k, c in metric_counts.items() if c > 1)
    if metric_dupes:
        flags.append("duplicate_metric")
        notes.append("duplicate_metric: " + ", ".join(metric_dupes))

    subtest_by_key = {}
    metric_by_key = {}
    if definition is not None:
        subtest_by_key = {s.key: s for s in definition.subtests}
        metric_by_key = {m.key: m for m in definition.overall_metrics}

        unknown_subtests = sorted(
            k for k in key_counts if canonical_subtest_key(definition, k) is None
        )
        if unknown_subtests:
            flags.append("unknown_subtest")
            notes.append("unknown_subtest: " + ", ".join(unknown_subtests))

        unknown_metrics = sorted(k for k in metric_counts if k not in metric_by_key)
        if unknown_metrics:
            flags.append("unknown_metric")
            notes.append("unknown_metric: " + ", ".join(unknown_metrics))

        missing_required = sorted(
            m.key
            for m in definition.overall_metrics
            if m.required and m.key not in metric_counts
        )
        if missing_required:
            flags.append("missing_required_metric")
            notes.append("missing_required_metric: " + ", ".join(missing_required))

    # rw_exceeds_max + metric_unparseable-on-subtest checks (test-agnostic,
    # richer with a definition).
    rw_exceeds: list[str] = []
    unparseable_subtests: list[str] = []
    subtest_rw_values: dict[str, float] = {}
    for st in extracted.subtests:
        rw = coerce_float(st.rw)
        if st.rw is not None and rw is None:
            unparseable_subtests.append(f"subtest:{st.key}.rw={st.rw!r}")
        rw_max = coerce_float(st.rw_max)
        canonical = canonical_subtest_key(definition, st.key) if definition else None
        if rw_max is None and canonical is not None and canonical in subtest_by_key:
            rw_max = subtest_by_key[canonical].rw_max
        if rw is not None and rw_max is not None and rw > rw_max:
            rw_exceeds.append(f"{st.key}: rw={rw} > rw_max={rw_max}")
        if rw is not None:
            subtest_rw_values[canonical or st.key] = (
                subtest_rw_values.get(canonical or st.key, 0.0) + rw
            )
    if rw_exceeds:
        flags.append("rw_exceeds_max")
        notes.append("rw_exceeds_max: " + "; ".join(rw_exceeds))

    unparseable_metrics: list[str] = []
    metric_num_values: dict[str, float] = {}
    for m in extracted.overall:
        value_type = metric_by_key[m.key].value_type if m.key in metric_by_key else None
        if value_type in ("int", "real"):
            num = coerce_float(m.value)
            if m.value is not None and num is None:
                unparseable_metrics.append(f"metric:{m.key}={m.value!r}")
            elif num is not None:
                metric_num_values[m.key] = num
    if unparseable_subtests or unparseable_metrics:
        flags.append("metric_unparseable")
        notes.append(
            "metric_unparseable: "
            + "; ".join(unparseable_subtests + unparseable_metrics)
        )

    # metric_out_of_range (definition-driven min_value/max_value)
    out_of_range: list[str] = []
    if definition is not None:
        for m in extracted.overall:
            defn_metric = metric_by_key.get(m.key)
            if defn_metric is None:
                continue
            num = metric_num_values.get(m.key)
            if num is None:
                continue
            if defn_metric.min_value is not None and num < defn_metric.min_value:
                out_of_range.append(f"{m.key}={num} < min={defn_metric.min_value}")
            if defn_metric.max_value is not None and num > defn_metric.max_value:
                out_of_range.append(f"{m.key}={num} > max={defn_metric.max_value}")
    if out_of_range:
        flags.append("metric_out_of_range")
        notes.append("metric_out_of_range: " + "; ".join(out_of_range))

    # [[rules.sum]] — tolerance-based, now that raw scores are floats. Each
    # rule sums only its own `over` set (ADR 0015); the legacy
    # "subtests.rw" form keeps summing every emitted row.
    if definition is not None:
        mismatch_notes = _sum_rule_mismatches(
            extracted, definition, subtest_rw_values, metric_num_values
        )
        if mismatch_notes:
            flags.append("total_mismatch")
            notes.extend(mismatch_notes)

    if extracted.test_date_raw:
        parsed: date | None = None
        if extracted.test_date_iso:
            try:
                parsed = date.fromisoformat(extracted.test_date_iso)
            except ValueError:
                parsed = None
        if parsed is None:
            flags.append("date_unparseable")
            notes.append(f"date_unparseable: raw={extracted.test_date_raw!r}")
        else:
            max_date = _max_plausible_date(today)
            if not (_MIN_PLAUSIBLE_DATE <= parsed <= max_date):
                flags.append("date_implausible")
                notes.append(f"date_implausible: parsed={parsed.isoformat()}")

    has_blocking_flags = any(is_blocking_flag(f) for f in flags)
    review_notes = "; ".join(notes) if notes else None
    return ValidationResult(
        flags=flags, has_blocking_flags=has_blocking_flags, review_notes=review_notes
    )
