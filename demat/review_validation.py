"""Review-time validation (assessment review "Validation" section).

Four checks, run on the *current* DB state of an assessment (post-edit, not
the frozen extraction-time `flags`):

1. `values_consistent` — deterministic (reuses `[[rules.sum]]`/rw_max/
   required-metric rules, same source of truth as `demat/validation.py`).
2. `norm_mapping` — deterministic: RW -> PR/T via the external knowledge
   base (`demat/reports/norms.py`), for the assessment's own class/gender
   norm group. Never computed by an LLM (ADR 0008's KB rule 2).
3. `note_matches_mapping` — the one genuinely semantic check: an LLM
   compares the free-text assessment note against the *Python-derived*
   classification. The LLM never computes the classification itself, only
   judges agreement (same "agent judges, Python computes" split as
   `demat/reports/prose.py`).
4. `norm_completion` — read-only: reports what `demat/norm_completion.py`
   already wrote to `pr_provenance`/`value_provenance` (ADR 0013). Never
   computes or writes a value itself.

A `KnowledgeBaseUnavailableError` or `RunnerUnavailableError` — infrastructure
missing, not a finding about this assessment — downgrades to a `warning`
instead of an `error`, per ADR 0005 §9.6 ("never crash, never fall back to a
guessed value").
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from demat import config, norm_variants
from demat.agent_runner import RunnerUnavailableError, SkillRunner, SkillRunnerError
from demat.reports import norms
from demat.reports.norms import KnowledgeBaseUnavailableError
from demat.test_registry import TestDefinition

Severity = Literal["error", "warning", "info"]

NOTE_CHECK_SKILL = "demat-review-note-check"

_GENDER_GROUP_SUFFIX = {"junge": "jungen", "maedchen": "mädchen"}
_SUM_TOLERANCE = 0.01
_NUM_TOLERANCE = 0.05


@dataclass
class ReviewFinding:
    check: str
    severity: Severity
    message: str
    # Namespaced pointer to the field this finding is about — "subtest:<key>",
    # "metric:<key>", or "field:<name>" for a scalar assessment field (e.g.
    # "field:norm_variant", "field:assessment_note") — so the review panel can
    # render it next to that field instead of only in the top-of-panel list.
    # None for a finding with no single field to anchor to. Defaulted so old
    # persisted `findings_to_json` output (written before this field existed)
    # still round-trips through `findings_from_json`.
    field_ref: str | None = None


@dataclass
class ReviewValidationResult:
    findings: list[ReviewFinding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)


def findings_to_json(findings: list[ReviewFinding]) -> str:
    return json.dumps([asdict(f) for f in findings])


def findings_from_json(raw: str | None) -> list[ReviewFinding]:
    if not raw:
        return []
    return [ReviewFinding(**f) for f in json.loads(raw)]


class NoteCheckResult(BaseModel):
    matches: bool
    explanation: str


# ---------------------------------------------------------------------------
# Check 1 — values noted are correct and add up.
# ---------------------------------------------------------------------------


def _check_values_consistent(
    metrics_rows: list[dict[str, Any]],
    subtest_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if definition is None:
        return findings

    subtest_by_key = {s.key: s for s in definition.subtests}
    metric_row_by_key = {r["metric_key"]: r for r in metrics_rows}

    subtest_rw_total = 0.0
    any_rw = False
    for row in subtest_rows:
        key = row["subtest_key"]
        rw = row.get("rw")
        if rw is None:
            continue
        any_rw = True
        subtest_rw_total += rw
        subtest_def = subtest_by_key.get(key)
        rw_max = row.get("rw_max")
        if rw_max is None and subtest_def is not None:
            rw_max = subtest_def.rw_max
        if rw_max is not None and rw > rw_max:
            findings.append(
                ReviewFinding(
                    "values_consistent",
                    "error",
                    f"Subtest {key}: RW={rw} exceeds the maximum ({rw_max}).",
                    field_ref=f"subtest:{key}",
                )
            )

    # Per-rule sums (ADR 0015): a list-form rule sums only its own `over`
    # keys; the legacy "subtests.rw" form keeps summing every stored row.
    # Keys stored more than once are ambiguous and skipped.
    key_counts: dict[str, int] = {}
    rw_by_key: dict[str, float] = {}
    for row in subtest_rows:
        key = row["subtest_key"]
        key_counts[key] = key_counts.get(key, 0) + 1
        if row.get("rw") is not None:
            rw_by_key[key] = row["rw"]
    for key, count in key_counts.items():
        if count > 1:
            rw_by_key.pop(key, None)
    subtest_rules = {r.subtest: r for r in definition.sum_rules_subtest()}

    def component_sum(keys: list[str]) -> float | None:
        if all(k in rw_by_key for k in keys):
            return sum(rw_by_key[k] for k in keys)
        return None

    for rule in definition.rules_sum:
        if rule.is_legacy:
            if not any_rw:
                continue
            assert rule.metric is not None
            metric_row = metric_row_by_key.get(rule.metric)
            total = metric_row.get("value_num") if metric_row else None
            if total is None:
                continue
            if abs(subtest_rw_total - total) > _SUM_TOLERANCE:
                findings.append(
                    ReviewFinding(
                        "values_consistent",
                        "error",
                        f"Subtest RW values sum to {subtest_rw_total}, but "
                        f"{rule.metric} is {total}.",
                        field_ref=f"metric:{rule.metric}",
                    )
                )
            continue

        over = definition.over_keys(rule)
        if rule.subtest is not None:
            target_rw = rw_by_key.get(rule.subtest)
            summed = component_sum(over)
            if target_rw is None or summed is None:
                continue
            if abs(summed - target_rw) > _SUM_TOLERANCE:
                findings.append(
                    ReviewFinding(
                        "values_consistent",
                        "error",
                        f"Subtest {rule.subtest}: RW={target_rw}, but "
                        f"{'+'.join(over)} sum to {summed}.",
                        field_ref=f"subtest:{rule.subtest}",
                    )
                )
            continue

        assert rule.metric is not None
        metric_row = metric_row_by_key.get(rule.metric)
        total = metric_row.get("value_num") if metric_row else None
        if total is None:
            continue
        summands: list[float] = []
        for k in over:
            if k in rw_by_key:
                summands.append(rw_by_key[k])
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
            metric_sum = sum(summands)
            if abs(metric_sum - total) > _SUM_TOLERANCE:
                findings.append(
                    ReviewFinding(
                        "values_consistent",
                        "error",
                        f"Subtest RW values sum to {metric_sum}, but "
                        f"{rule.metric} is {total}.",
                        field_ref=f"metric:{rule.metric}",
                    )
                )

    for m in definition.overall_metrics:
        overall_row = metric_row_by_key.get(m.key)
        if m.required and (
            overall_row is None or overall_row.get("value_raw") in (None, "")
        ):
            findings.append(
                ReviewFinding(
                    "values_consistent",
                    "error",
                    f"Required metric {m.key} ({m.label}) has no value.",
                    field_ref=f"metric:{m.key}",
                )
            )
            continue
        if overall_row is None or overall_row.get("value_num") is None:
            continue
        num = overall_row["value_num"]
        if m.min_value is not None and num < m.min_value:
            findings.append(
                ReviewFinding(
                    "values_consistent",
                    "error",
                    f"{m.key}={num} is below the allowed minimum ({m.min_value}).",
                    field_ref=f"metric:{m.key}",
                )
            )
        if m.max_value is not None and num > m.max_value:
            findings.append(
                ReviewFinding(
                    "values_consistent",
                    "error",
                    f"{m.key}={num} is above the allowed maximum ({m.max_value}).",
                    field_ref=f"metric:{m.key}",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 2 — RW values are correctly mapped for the test's class/gender.
# ---------------------------------------------------------------------------


def _coerce_kb_float(value: Any) -> float | None:
    """Best-effort float coercion of a `lookup.py` cell value. Returns None
    for a non-numeric literal (e.g. "≤1") instead of raising — the caller
    treats that candidate as not comparable, never as a crash.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mismatch_message(
    label: str, stored: Any, candidates: list[tuple[str, Any]]
) -> str:
    """`candidates`: (group, kb_value) pairs, none of which matched `stored`."""
    if len(candidates) == 1:
        group, value = candidates[0]
        return f"{label}: stored value {stored!r} but RW maps to {value!r} for norm group {group}."
    mapped = ", ".join(f"{g}={v!r}" for g, v in candidates)
    return f"{label}: stored value {stored!r} matches neither candidate norm group ({mapped})."


def _subtest_mismatch_message(
    key: str, stored_pr: int, raw: float, candidates: list[tuple[str, Any]]
) -> str:
    """`candidates`: (group, kb_pr) pairs, none of which matched `stored_pr`."""
    if len(candidates) == 1:
        group, kb_pr = candidates[0]
        return (
            f"Subtest {key}: stored PR={stored_pr} but RW={raw} maps to PR={kb_pr} "
            f"for norm group {group}."
        )
    mapped = ", ".join(f"{g}={v}" for g, v in candidates)
    return (
        f"Subtest {key}: stored PR={stored_pr} (RW={raw}) matches neither candidate "
        f"norm group ({mapped})."
    )


def _misprint_warning(
    label: str,
    matches: list[tuple[str, dict[str, Any]]],
    field_ref: str | None = None,
) -> ReviewFinding | None:
    """A `warning` if every matching candidate's manual cell is itself
    flagged as a probable misprint (`allow_flagged=True` still returns a
    value for one) — the stored value isn't wrong, but it traces back to a
    cell the KB curator couldn't fully trust either.
    """
    if matches and all(r.get("status") == "druckfehler_vermutet" for _, r in matches):
        groups_str = ", ".join(g for g, _ in matches)
        return ReviewFinding(
            "norm_mapping",
            "warning",
            f"{label}: stored value matches norm group {groups_str}, but the manual "
            "flags that cell as a probable misprint — verify in the paper manual if "
            "decisive.",
            field_ref=field_ref,
        )
    return None


def _check_norm_mapping(
    assessment: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
    subtest_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if definition is None or definition.norm_test is None:
        return findings
    norm_test = definition.norm_test

    norm_variant = assessment.get("norm_variant")
    if not norm_variant:
        findings.append(
            ReviewFinding(
                "norm_mapping",
                "warning",
                "Normtabelle (Normgruppe) für diese Auswertung nicht "
                "angegeben — die RW→PR-Prüfung wurde übersprungen. Bitte "
                "die verwendete Normtabelle setzen.",
                field_ref="field:norm_variant",
            )
        )
        return findings

    try:
        group, _label, variant = norms.resolve_norm_group(norm_test, norm_variant)
    except ValueError as exc:
        findings.append(
            ReviewFinding(
                "norm_mapping", "warning", str(exc), field_ref="field:norm_variant"
            )
        )
        return findings

    cross_check_warning = norms.grade_cross_check_warning(
        assessment.get("klasse"), norm_test, variant, group
    )
    if cross_check_warning is not None:
        findings.append(
            ReviewFinding(
                "norm_mapping",
                "warning",
                cross_check_warning,
                field_ref="field:norm_variant",
            )
        )

    groups = [group]
    if norm_variants.GENDER_SPLIT.get(norm_test, False):
        suffix = _GENDER_GROUP_SUFFIX.get(assessment.get("gender") or "")
        if suffix is None:
            findings.append(
                ReviewFinding(
                    "norm_mapping",
                    "warning",
                    f"Gender {assessment.get('gender')!r} not recognized; RW->PR "
                    f"mapping checked against the combined ({group}) norm group "
                    "only, not the gender-specific one.",
                    field_ref="field:gender",
                )
            )
        else:
            # The manual publishes both a combined and a gender-specific
            # table for the same raw score; which one a given sheet was
            # actually scored against is not recorded (unlike `norm_variant`
            # itself, ADR 0010) and is not reliably the gendered one — real
            # data has both in use. Check both candidates and only flag a
            # value that matches neither, rather than guessing one table.
            groups.append(group.replace("_gesamt", f"_{suffix}"))

    kb_unavailable_warned = False

    def lookup_one(scale: str, group_name: str, raw: float) -> dict[str, Any] | None:
        nonlocal kb_unavailable_warned
        try:
            # allow_flagged=True: a druckfehler_vermutet cell still yields a
            # comparable value here — a match against it becomes a
            # `_misprint_warning`, not a silent pass and not a hard block.
            return norms.norm(norm_test, scale, group_name, raw, allow_flagged=True)
        except KnowledgeBaseUnavailableError as exc:
            if not kb_unavailable_warned:
                findings.append(
                    ReviewFinding(
                        "norm_mapping", "warning", f"Knowledge base unavailable: {exc}"
                    )
                )
                kb_unavailable_warned = True
            return None
        except RuntimeError:
            # Not valid in this candidate table (e.g. a raw score step it
            # doesn't print) — the other candidate, if any, may still work.
            return None

    def lookup(scale: str, raw: float) -> list[tuple[str, dict[str, Any]]]:
        """Every candidate group's result. `leer_im_original` (the manual
        prints no value for *some* field at this row, e.g. no PR at a very
        low raw score) is not filtered out here — it still carries other,
        genuinely-printed fields (e.g. T-Wert/T-Band); each per-field
        comparison below skips only the specific field that's actually
        `None`, not the whole row.
        """
        results = []
        for g in groups:
            result = lookup_one(scale, g, raw)
            if result is not None:
                results.append((g, result))
        return results

    subtest_row_by_key = {r["subtest_key"]: r for r in subtest_rows}
    for s in definition.subtests:
        if s.norm_scale is None:
            continue
        row = subtest_row_by_key.get(s.key)
        if row is None or row.get("rw") is None or row.get("pr") is None:
            continue
        candidates = lookup(s.norm_scale, row["rw"])
        if not candidates:
            continue
        stored_pr = int(row["pr"])
        # Defensive: no current KB subtest series has a non-numeric `pr`
        # (unlike Gesamtwert `pr_total`, which can print "≤1") — guard the
        # float(...) call anyway, so a candidate cell like that is skipped
        # rather than crashing the whole check.
        all_values = [
            (g, r["pr"])
            for g, r in candidates
            if r.get("pr") is not None and _coerce_kb_float(r["pr"]) is not None
        ]
        if not all_values:
            continue
        matches = [
            (g, r)
            for g, r in candidates
            if r.get("pr") is not None
            and (kb_pr := _coerce_kb_float(r["pr"])) is not None
            and stored_pr == round(kb_pr)
        ]
        if matches:
            warn = _misprint_warning(
                f"Subtest {s.key} PR", matches, field_ref=f"subtest:{s.key}"
            )
            if warn:
                findings.append(warn)
            continue
        findings.append(
            ReviewFinding(
                "norm_mapping",
                "error",
                _subtest_mismatch_message(s.key, stored_pr, row["rw"], all_values),
                field_ref=f"subtest:{s.key}",
            )
        )

    metric_row_by_key = {r["metric_key"]: r for r in metrics_rows}
    base_metrics = {
        m.norm_scale: m
        for m in definition.overall_metrics
        if m.norm_scale and m.norm_field is None
    }
    for scale, base_metric in base_metrics.items():
        base_row = metric_row_by_key.get(base_metric.key)
        if base_row is None or base_row.get("value_num") is None:
            continue
        candidates = lookup(scale, base_row["value_num"])
        if not candidates:
            continue
        for m in definition.overall_metrics:
            if m.norm_scale != scale or m.norm_field is None:
                continue
            row = metric_row_by_key.get(m.key)
            if row is None:
                continue
            # A stored value whose `value_num` is NULL is either a text
            # metric or a numeric one holding a non-numeric literal verbatim
            # (e.g. `pr_total = "≤1"`, ADR 0013) — compare as text either
            # way, rather than crashing on `float("≤1")`.
            if m.value_type == "text" or row.get("value_num") is None:
                stored = row.get("value_text") or row.get("value_raw")
                if stored is None:
                    continue
                stored_str = str(stored).strip()
                all_values = [
                    (g, r[m.norm_field])
                    for g, r in candidates
                    if r.get(m.norm_field) is not None
                ]
                matches = [
                    (g, r)
                    for g, r in candidates
                    if r.get(m.norm_field) is not None
                    and str(r[m.norm_field]).strip() == stored_str
                ]
            else:
                stored = row.get("value_num")
                if stored is None:
                    continue
                all_values = []
                matches = []
                for g, r in candidates:
                    kb_value = r.get(m.norm_field)
                    if kb_value is None:
                        continue
                    # The candidate cell itself may hold a non-numeric
                    # literal (e.g. "≤1") — skip that candidate rather than
                    # crashing on `float(...)`; the other candidate, if any,
                    # may still be comparable.
                    kb_num = _coerce_kb_float(kb_value)
                    if kb_num is None:
                        continue
                    all_values.append((g, kb_value))
                    if abs(float(stored) - kb_num) <= _NUM_TOLERANCE:
                        matches.append((g, r))
            if not all_values:
                continue
            if matches:
                warn = _misprint_warning(m.key, matches, field_ref=f"metric:{m.key}")
                if warn:
                    findings.append(warn)
                continue
            findings.append(
                ReviewFinding(
                    "norm_mapping",
                    "error",
                    _mismatch_message(m.key, stored, all_values),
                    field_ref=f"metric:{m.key}",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 4 — reports what `demat/norm_completion.py` already wrote
# (ADR 0013). Reads stored rows only: no KB call, no write. The `group`
# name is resolved locally from the stored `norm_variant` (deterministic,
# no subprocess) purely to render it in the message.
# ---------------------------------------------------------------------------

_NORM_COMPLETION_BLANK_MESSAGE = "Nicht nachgeschlagen — Grund siehe Review-Notizen."


def _check_norm_completion(
    assessment: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
    subtest_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if definition is None or definition.norm_test is None:
        return findings

    norm_variant = assessment.get("norm_variant")
    group: str | None = None
    if norm_variant:
        try:
            group, _label, _variant = norms.resolve_norm_group(
                definition.norm_test, norm_variant
            )
        except ValueError:
            group = None

    metric_row_by_key = {r["metric_key"]: r for r in metrics_rows}
    rw_total_row = metric_row_by_key.get("rw_total")
    rw_total_display = rw_total_row.get("value_raw") if rw_total_row else None

    no_variant_count = 0
    norm_scale_by_key = {s.key: s.norm_scale for s in definition.subtests}
    subtest_rules = {r.subtest: r for r in definition.sum_rules_subtest()}

    for row in subtest_rows:
        key = row["subtest_key"]

        # RW sums (ADR 0015) — arithmetic, never needs a Normtabelle, so a
        # blank one is never folded into the "no variant" aggregate.
        rw_provenance = row.get("rw_provenance")
        if rw_provenance == "computed":
            rule = subtest_rules.get(key)
            keys = "+".join(definition.over_keys(rule)) if rule else "Einzelwerte"
            findings.append(
                ReviewFinding(
                    "norm_completion",
                    "info",
                    f"RW {row.get('value_raw')} als Summe von {keys}.",
                    field_ref=f"subtest:{key}",
                )
            )
        elif rw_provenance == "blank":
            findings.append(
                ReviewFinding(
                    "norm_completion",
                    "warning",
                    _NORM_COMPLETION_BLANK_MESSAGE,
                    field_ref=f"subtest:{key}",
                )
            )

        # A PR cell on a subtest with no norm scale (DEMAT 3+ Aufgabentyp
        # rows, DEMAT 5+ B) can only ever become 'unavailable' — never a
        # finding, even while still 'blank' because the KB is unavailable.
        if norm_scale_by_key.get(key) is None:
            continue

        provenance = row.get("pr_provenance")
        if provenance == "computed":
            findings.append(
                ReviewFinding(
                    "norm_completion",
                    "info",
                    f"PR {row.get('pr')} aus Normtabelle {group} (RW {row.get('rw')}).",
                    field_ref=f"subtest:{key}",
                )
            )
        elif provenance == "blank":
            if group is None:
                no_variant_count += 1
            else:
                findings.append(
                    ReviewFinding(
                        "norm_completion",
                        "warning",
                        _NORM_COMPLETION_BLANK_MESSAGE,
                        field_ref=f"subtest:{key}",
                    )
                )
        # 'unavailable', 'sheet', 'manual' and NULL/None cells: no finding.

    for m in definition.overall_metrics:
        metric_row = metric_row_by_key.get(m.key)
        if metric_row is None:
            continue
        provenance = metric_row.get("value_provenance")
        key = m.key
        if provenance == "computed":
            value_display = metric_row.get("value_raw")
            if key == "rw_total":
                message = f"RW(ges) {value_display} als Summe der Subtest-RW."
            else:
                message = (
                    f"{key} {value_display} aus Normtabelle {group} "
                    f"(RW(ges) {rw_total_display})."
                )
            findings.append(
                ReviewFinding(
                    "norm_completion", "info", message, field_ref=f"metric:{key}"
                )
            )
        elif provenance == "blank":
            # rw_total's sum-fill never needs a norm_variant (ADR 0013), so
            # a blank rw_total is never a "no variant" reason — it always
            # gets its own per-cell warning, even when `group` is None.
            if group is None and key != "rw_total":
                no_variant_count += 1
            else:
                findings.append(
                    ReviewFinding(
                        "norm_completion",
                        "warning",
                        _NORM_COMPLETION_BLANK_MESSAGE,
                        field_ref=f"metric:{key}",
                    )
                )

    if no_variant_count:
        findings.append(
            ReviewFinding(
                "norm_completion",
                "warning",
                f"{no_variant_count} Normwert(e) können nachgeschlagen werden, "
                "sobald eine Normtabelle gesetzt ist.",
                field_ref="field:norm_variant",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Check 3 — the assessment note matches the RW/PR mapping. The only check
# that calls an LLM, and only to judge agreement with a classification
# Python already computed — never to derive it.
# ---------------------------------------------------------------------------


def _build_note_check_prompt(category: str, basis: str, value: float, note: str) -> str:
    basis_label = "Prozentrang (PR)" if basis == "pr" else "T-Wert"
    return (
        "This DEMAT assessment has been classified using the test manual's published "
        f"norm tables (computed deterministically, not by you): category={category!r}, "
        f"basis={basis_label}, value={value}.\n\n"
        "Below is the free-text assessment note a teacher/psychologist wrote for this "
        "child. Decide whether the note's characterization of the child's performance "
        "is consistent with this classification (e.g. a note describing weak or "
        "struggling performance should not accompany an 'überdurchschnittlich' "
        "classification, and a note describing strong performance should not "
        "accompany a 'schwach' or 'unterdurchschnittlich' classification). Minor "
        "nuance and a mixed picture across subtests is fine — flag only a clear "
        "contradiction between the note's overall tone and the classification.\n\n"
        f"Assessment note:\n{note}"
    )


def _check_note_matches_mapping(
    assessment: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
    runner: SkillRunner,
    run_dir: Path,
    stop_event: threading.Event | None = None,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    note = (assessment.get("assessment_note") or "").strip()
    if not note or definition is None or definition.norm_test is None:
        return findings

    try:
        basis = norms.basis_for(definition)
    except KnowledgeBaseUnavailableError as exc:
        findings.append(
            ReviewFinding(
                "note_matches_mapping",
                "warning",
                f"Knowledge base unavailable: {exc}",
                field_ref="field:assessment_note",
            )
        )
        return findings

    norm_field = "pr" if basis == "pr" else "t_wert"
    metric_row_by_key = {r["metric_key"]: r for r in metrics_rows}
    total_value = None
    for m in definition.overall_metrics:
        if m.norm_field != norm_field:
            continue
        row = metric_row_by_key.get(m.key)
        if row is not None and row.get("value_num") is not None:
            total_value = row["value_num"]
            break
    if total_value is None:
        findings.append(
            ReviewFinding(
                "note_matches_mapping",
                "warning",
                f"Cannot verify the note against a classification: no {basis} total "
                "value is stored.",
                field_ref="field:assessment_note",
            )
        )
        return findings

    kwargs = {"pr": total_value} if basis == "pr" else {"t": total_value}
    try:
        classification = norms.classify(definition.norm_test, **kwargs)
    except KnowledgeBaseUnavailableError as exc:
        findings.append(
            ReviewFinding(
                "note_matches_mapping",
                "warning",
                f"Knowledge base unavailable: {exc}",
                field_ref="field:assessment_note",
            )
        )
        return findings

    category = classification.get("category")
    if category is None:
        return findings

    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "note_check.json"
    prompt = _build_note_check_prompt(category, basis, total_value, note)
    try:
        raw = runner.run_text(
            prompt,
            str(output_path),
            NoteCheckResult,
            skill_name=NOTE_CHECK_SKILL,
            stop_event=stop_event,
        )
        result = NoteCheckResult.model_validate(raw)
    except (SkillRunnerError, RunnerUnavailableError) as exc:
        findings.append(
            ReviewFinding(
                "note_matches_mapping",
                "warning",
                f"Could not verify the assessment note against the classification "
                f"(agent unavailable): {exc}",
                field_ref="field:assessment_note",
            )
        )
        return findings

    if not result.matches:
        findings.append(
            ReviewFinding(
                "note_matches_mapping",
                "error",
                f"Assessment note may not match the classification ({category}): "
                f"{result.explanation}",
                field_ref="field:assessment_note",
            )
        )
    return findings


def run_review_validations(
    assessment: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
    subtest_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
    runner: SkillRunner,
    stop_event: threading.Event | None = None,
) -> ReviewValidationResult:
    """Run all three review-time validations against the current DB state."""
    findings: list[ReviewFinding] = []
    findings += _check_values_consistent(metrics_rows, subtest_rows, definition)
    findings += _check_norm_mapping(assessment, metrics_rows, subtest_rows, definition)
    findings += _check_norm_completion(
        assessment, metrics_rows, subtest_rows, definition
    )
    run_dir = config.RUNS_DIR / f"review_validation_{assessment['assessment_id']}"
    findings += _check_note_matches_mapping(
        assessment, metrics_rows, definition, runner, run_dir, stop_event=stop_event
    )
    return ReviewValidationResult(findings=findings)
