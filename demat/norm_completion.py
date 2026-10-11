"""Fill the PR/PR-Band/T/T-Band norm cells a DEMAT sheet leaves for the
evaluator, from the knowledge base (ADR 0013).

`plan_completion` is pure (a `lookup` function is injected, defaulting to
`demat.reports.norms.norm` with `allow_flagged=True`) and never touches the
database. `run_completion` reads the current DB state for one assessment,
calls `plan_completion`, and persists the result via
`demat.store.apply_norm_completion`.

Only Python performs the lookup, and only through `demat.reports.norms`,
which is the only caller of `scripts/lookup.py` (KB rule 2, ADR 0008) — no
LLM produces, sees or chooses a norm value here.

Never overwrites a `sheet` or `manual` cell (I1): only cells whose stored
provenance is already `blank`, `computed` or `unavailable`
(`store.COMPLETION_OWNED`) are touched, and only on a `needs_review` row
(I2). If the knowledge base is unavailable, nothing is written and nothing
is cleared (I4).

It also fills blank Rohwert-Summe cells by arithmetic from the definition's
`[[rules.sum]]` rules — RW(ges), and on DEMAT 3+ the subtest RWs ARI/SAR/GEO
summed from their Aufgabentyp rows (ADR 0015) — through one effective RW map
that the PR/Gesamtwert lookups then read from.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from demat import norm_variants, store
from demat.reports import norms
from demat.reports.norms import KnowledgeBaseUnavailableError
from demat.test_registry import TestDefinition
from demat.validation import coerce_float

LookupFn = Any  # Callable[[str, str, str, float], dict[str, Any]]

_DEFAULT_LOOKUP = functools.partial(norms.norm, allow_flagged=True)

SkipReason = Literal[
    "no_norm_variant",
    "invalid_norm_variant",
    "rw_missing",
    "rw_not_in_table",
    "cell_misprint",
    "not_published",
    "no_norm_scale",
    "sum_incomplete",
]

_SKIP_MESSAGES: dict[SkipReason, str] = {
    "no_norm_variant": (
        "Keine Normtabelle gesetzt — dieser Wert kann noch nicht nachgeschlagen werden."
    ),
    "invalid_norm_variant": (
        "Die gesetzte Normtabelle ist für diesen Test nicht gültig."
    ),
    "rw_missing": "Kein Rohwert vorhanden — dieser Wert kann nicht nachgeschlagen werden.",
    "rw_not_in_table": "Rohwert nicht in der Normtabelle enthalten.",
    "cell_misprint": (
        "Zelle im Manual als vermutlicher Druckfehler markiert — im "
        "Papier-Manual nachschlagen."
    ),
    "not_published": "Wert im Manual für diesen Rohwert nicht angegeben.",
    "no_norm_scale": "Für diesen Subtest gibt es keine Normwerte.",
    "sum_incomplete": (
        "RW(ges) kann nicht durch Summierung der Subtest-RW ermittelt werden."
    ),
}


@dataclass(frozen=True)
class CellWrite:
    table: Literal["subtest", "metric"]
    position: int
    field_ref: str  # "subtest:ZE" | "metric:pr_total"
    provenance: Literal["computed", "blank", "unavailable"]
    pr: int | None = None  # subtest only
    value_raw: str | None = None
    value_num: float | None = None
    value_text: str | None = None
    detail: str = ""
    # Which cell of the row: "pr"/"rw" on a subtest (an RW sum, ADR 0015,
    # vs. a PR lookup on the same subtest), "value" on a metric.
    field: Literal["pr", "rw", "value"] = "pr"
    rw: float | None = None  # subtest RW writes; value_raw carries the German string


@dataclass(frozen=True)
class SkippedCell:
    field_ref: str
    reason: SkipReason
    message: str


@dataclass
class CompletionPlan:
    writes: list[CellWrite] = field(default_factory=list)
    skipped: list[SkippedCell] = field(default_factory=list)
    norm_group: str | None = None
    kb_unavailable: str | None = None


def _skip(field_ref: str, reason: SkipReason, *, extra: str = "") -> SkippedCell:
    message = _SKIP_MESSAGES[reason]
    if extra:
        message = f"{message} ({extra})"
    return SkippedCell(field_ref=field_ref, reason=reason, message=message)


def _format_german(value: float) -> str:
    """`20` -> `"20"`, `20.5` -> `"20,5"` — matches how a scorer would write
    a raw-score sum on the sheet (reference.md's "half points" rule).
    """
    dec = Decimal(str(value)).normalize()
    text = format(dec, "f")
    if "." in text:
        whole, frac = text.split(".")
        frac = frac.rstrip("0")
        text = f"{whole},{frac}" if frac else whole
    return text


def _floats_equal(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


def _subtest_write(
    row: dict[str, Any],
    key: str,
    *,
    provenance: Literal["computed", "blank", "unavailable"],
    pr: int | None,
    detail: str = "",
) -> CellWrite | None:
    """None if this write would not change the stored cell (idempotent
    re-run, "emit only changes")."""
    current_provenance = row.get("pr_provenance") or "sheet"
    current_pr = row.get("pr")
    if current_provenance == provenance and current_pr == pr:
        return None
    return CellWrite(
        table="subtest",
        position=row["position"],
        field_ref=f"subtest:{key}",
        provenance=provenance,
        pr=pr,
        detail=detail,
    )


def _metric_write(
    row: dict[str, Any],
    key: str,
    *,
    provenance: Literal["computed", "blank", "unavailable"],
    value_raw: str | None,
    value_num: float | None,
    value_text: str | None,
    detail: str = "",
) -> CellWrite | None:
    current_provenance = row.get("value_provenance") or "sheet"
    if (
        current_provenance == provenance
        and row.get("value_raw") == value_raw
        and _floats_equal(row.get("value_num"), value_num)
        and row.get("value_text") == value_text
    ):
        return None
    return CellWrite(
        table="metric",
        position=row["position"],
        field_ref=f"metric:{key}",
        provenance=provenance,
        value_raw=value_raw,
        value_num=value_num,
        value_text=value_text,
        detail=detail,
        field="value",
    )


def _subtest_rw_write(
    row: dict[str, Any],
    key: str,
    *,
    provenance: Literal["computed", "blank"],
    rw: float | None,
    detail: str = "",
) -> CellWrite | None:
    """An RW sum on a sum-target subtest (ADR 0015). None if unchanged."""
    value_raw = _format_german(rw) if rw is not None else None
    current_provenance = row.get("rw_provenance") or "sheet"
    if (
        current_provenance == provenance
        and _floats_equal(row.get("rw"), rw)
        and row.get("value_raw") == value_raw
    ):
        return None
    return CellWrite(
        table="subtest",
        position=row["position"],
        field_ref=f"subtest:{key}",
        provenance=provenance,
        value_raw=value_raw,
        detail=detail,
        field="rw",
        rw=rw,
    )


def plan_completion(
    assessment: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
    subtest_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
    *,
    lookup: LookupFn = _DEFAULT_LOOKUP,
) -> CompletionPlan:
    """Deterministic planner. Never touches the database; the caller
    (`run_completion`) applies the result via `store.apply_norm_completion`.
    """
    plan = CompletionPlan()

    if definition is None or definition.norm_test is None:
        return plan
    if assessment.get("status") != "needs_review":
        return plan

    metric_row_by_key = {r["metric_key"]: r for r in metrics_rows}
    subtest_row_by_key = {r["subtest_key"]: r for r in subtest_rows}

    def owned(row: dict[str, Any], prov_key: str) -> bool:
        return (row.get(prov_key) or "sheet") in store.COMPLETION_OWNED

    any_owned = (
        any(owned(r, "value_provenance") for r in metrics_rows)
        or any(owned(r, "pr_provenance") for r in subtest_rows)
        or any(owned(r, "rw_provenance") for r in subtest_rows)
    )
    if not any_owned:
        return plan

    norm_test = definition.norm_test

    # --- Step 1: effective RW map (ADR 0015) -------------------------------
    # `eff[key]` is the stored RW of the last row with that key (the same
    # last-row-wins as `subtest_row_by_key`, so PR lookups on 1+/2+/5+ are
    # unchanged); sum steps below overwrite it with what they compute, and
    # every later step reads from it (I10: no `rw_missing` for an RW this
    # same plan computes).
    counts: dict[str, int] = {}
    eff: dict[str, float | None] = {}
    for r in subtest_rows:
        k = r["subtest_key"]
        counts[k] = counts.get(k, 0) + 1
        eff[k] = r.get("rw")

    def components_ready(keys: list[str]) -> bool:
        return all(counts.get(k) == 1 and eff.get(k) is not None for k in keys)

    # --- Step 2: subtest-target sums (no KB call, no variant needed) -------
    for rule in definition.sum_rules_subtest():
        target = rule.subtest
        assert target is not None
        target_row = subtest_row_by_key.get(target)
        if target_row is None or counts.get(target, 0) > 1:
            continue
        if not owned(target_row, "rw_provenance"):
            continue
        over = definition.over_keys(rule)
        joined = "+".join(over)
        if components_ready(over):
            total = sum(v for k in over if (v := eff[k]) is not None)
            rw_write = _subtest_rw_write(
                target_row,
                target,
                provenance="computed",
                rw=total,
                detail=f"RW {_format_german(total)} als Summe von {joined}.",
            )
            eff[target] = total
        else:
            rw_write = _subtest_rw_write(
                target_row, target, provenance="blank", rw=None
            )
            plan.skipped.append(
                _skip(f"subtest:{target}", "sum_incomplete", extra=joined)
            )
            eff[target] = None
        if rw_write is not None:
            plan.writes.append(rw_write)

    # --- Step 3: metric-target sums (RW(ges)) ------------------------------
    rw_total_value_num: float | None = None
    rw_total_row = metric_row_by_key.get("rw_total")
    if rw_total_row is not None:
        rw_total_value_num = rw_total_row.get("value_num")

    for rule in definition.sum_rules_metric():
        assert rule.metric is not None
        metric_row = metric_row_by_key.get(rule.metric)
        if metric_row is None or not owned(metric_row, "value_provenance"):
            continue

        over = definition.over_keys(rule)
        if rule.is_legacy:
            # ADR 0013's preconditions, unchanged: every defined subtest
            # present exactly once with an RW, and no undefined key.
            complete = all(k in over for k in counts) and components_ready(over)
            detail_suffix = "als Summe der Subtest-RW."
        else:
            complete = components_ready(over)
            detail_suffix = f"als Summe von {'+'.join(over)}."

        if complete:
            total = sum(v for k in over if (v := eff[k]) is not None)
            value_raw = _format_german(total)
            write = _metric_write(
                metric_row,
                rule.metric,
                provenance="computed",
                value_raw=value_raw,
                value_num=total,
                value_text=None,
                detail=f"RW(ges) {value_raw} {detail_suffix}",
            )
            if rule.metric == "rw_total":
                rw_total_value_num = total
        else:
            write = _metric_write(
                metric_row,
                rule.metric,
                provenance="blank",
                value_raw=None,
                value_num=None,
                value_text=None,
            )
            plan.skipped.append(_skip(f"metric:{rule.metric}", "sum_incomplete"))
            if rule.metric == "rw_total":
                rw_total_value_num = None
        if write is not None:
            plan.writes.append(write)

    # --- Step 4: resolve the norm group -----------------------------------
    norm_variant = assessment.get("norm_variant")
    group: str | None = None
    variant_reason: SkipReason | None = None
    if not norm_variant:
        variant_reason = "no_norm_variant"
    elif not norm_variants.is_valid(norm_test, norm_variant):
        variant_reason = "invalid_norm_variant"
    else:
        try:
            group, _label, _variant = norms.resolve_norm_group(norm_test, norm_variant)
        except ValueError:
            variant_reason = "invalid_norm_variant"
    plan.norm_group = group

    # --- Step 5: subtest PRs ------------------------------------------------
    for s in definition.subtests:
        row = subtest_row_by_key.get(s.key)
        if row is None or not owned(row, "pr_provenance"):
            continue
        field_ref = f"subtest:{s.key}"

        if s.norm_scale is None:
            write = _subtest_write(row, s.key, provenance="unavailable", pr=None)
            plan.skipped.append(_skip(field_ref, "no_norm_scale"))
            if write is not None:
                plan.writes.append(write)
            continue

        rw = eff.get(s.key)
        if group is None:
            assert variant_reason is not None
            write = _subtest_write(row, s.key, provenance="blank", pr=None)
            plan.skipped.append(_skip(field_ref, variant_reason))
            if write is not None:
                plan.writes.append(write)
            continue

        if rw is None:
            write = _subtest_write(row, s.key, provenance="blank", pr=None)
            plan.skipped.append(_skip(field_ref, "rw_missing"))
            if write is not None:
                plan.writes.append(write)
            continue

        try:
            result = lookup(norm_test, s.norm_scale, group, rw)
        except KnowledgeBaseUnavailableError as exc:
            plan.writes = []
            plan.skipped = []
            plan.kb_unavailable = str(exc)
            return plan
        except RuntimeError as exc:
            write = _subtest_write(row, s.key, provenance="blank", pr=None)
            plan.skipped.append(_skip(field_ref, "rw_not_in_table", extra=str(exc)))
            if write is not None:
                plan.writes.append(write)
            continue

        status = result.get("status")
        if status == "druckfehler_vermutet":
            write = _subtest_write(row, s.key, provenance="blank", pr=None)
            plan.skipped.append(_skip(field_ref, "cell_misprint"))
            if write is not None:
                plan.writes.append(write)
            continue

        pr_value = result.get("pr")
        if pr_value is None:
            write = _subtest_write(row, s.key, provenance="unavailable", pr=None)
            plan.skipped.append(
                _skip(field_ref, "not_published", extra="PR im Manual nicht angegeben")
            )
            if write is not None:
                plan.writes.append(write)
            continue

        # Defensive only: no current KB row has a non-integer subtest PR
        # (they never have half-point steps) — a plain-digit check, not a
        # numeric round-trip, so "12.5" is refused rather than silently
        # rounded to 12 or 13.
        if not re.fullmatch(r"\d+", str(pr_value).strip()):
            write = _subtest_write(row, s.key, provenance="blank", pr=None)
            plan.skipped.append(
                _skip(field_ref, "rw_not_in_table", extra=f"pr={pr_value!r}")
            )
            if write is not None:
                plan.writes.append(write)
            continue
        pr_int = int(pr_value)

        detail = f"PR {pr_int} aus Normtabelle {group} (RW {rw})."
        if status == "unsicher":
            detail += " KB: blass gedruckt — ggf. im Papier-Manual prüfen."
        write = _subtest_write(
            row, s.key, provenance="computed", pr=pr_int, detail=detail
        )
        if write is not None:
            plan.writes.append(write)

    # --- Step 6: Gesamtwert fields ------------------------------------------
    base_metrics = {
        m.norm_scale: m
        for m in definition.overall_metrics
        if m.norm_scale and m.norm_field is None
    }
    for scale, base_metric in base_metrics.items():
        target_metrics = [
            m
            for m in definition.overall_metrics
            if m.norm_scale == scale and m.norm_field is not None
        ]
        if not target_metrics:
            continue

        base_value = (
            rw_total_value_num
            if base_metric.key == "rw_total"
            else (metric_row_by_key.get(base_metric.key) or {}).get("value_num")
        )

        owned_targets = [
            m
            for m in target_metrics
            if (row := metric_row_by_key.get(m.key)) is not None
            and owned(row, "value_provenance")
        ]
        if not owned_targets:
            continue

        if base_value is None:
            for m in owned_targets:
                row = metric_row_by_key[m.key]
                write = _metric_write(
                    row,
                    m.key,
                    provenance="blank",
                    value_raw=None,
                    value_num=None,
                    value_text=None,
                )
                plan.skipped.append(_skip(f"metric:{m.key}", "rw_missing"))
                if write is not None:
                    plan.writes.append(write)
            continue

        if group is None:
            assert variant_reason is not None
            for m in owned_targets:
                row = metric_row_by_key[m.key]
                write = _metric_write(
                    row,
                    m.key,
                    provenance="blank",
                    value_raw=None,
                    value_num=None,
                    value_text=None,
                )
                plan.skipped.append(_skip(f"metric:{m.key}", variant_reason))
                if write is not None:
                    plan.writes.append(write)
            continue

        try:
            result = lookup(norm_test, scale, group, base_value)
        except KnowledgeBaseUnavailableError as exc:
            plan.writes = []
            plan.skipped = []
            plan.kb_unavailable = str(exc)
            return plan
        except RuntimeError as exc:
            for m in owned_targets:
                row = metric_row_by_key[m.key]
                write = _metric_write(
                    row,
                    m.key,
                    provenance="blank",
                    value_raw=None,
                    value_num=None,
                    value_text=None,
                )
                plan.skipped.append(
                    _skip(f"metric:{m.key}", "rw_not_in_table", extra=str(exc))
                )
                if write is not None:
                    plan.writes.append(write)
            continue

        status = result.get("status")
        unsicher_suffix = (
            " KB: blass gedruckt — ggf. im Papier-Manual prüfen."
            if status == "unsicher"
            else ""
        )

        for m in owned_targets:
            row = metric_row_by_key[m.key]
            field_ref = f"metric:{m.key}"

            # The manual prints no value for this field at all (e.g. no
            # pr_band on DEMAT 1+/5+) regardless of the row's misprint
            # status — an absent value can't itself be a misprint, so this
            # check comes first (worked example: DEMAT 5+ gymnasium RW 22
            # is druckfehler_vermutet, but pr_band is still `unavailable`,
            # not `blank`).
            literal = result.get(m.norm_field)
            if literal is None:
                write = _metric_write(
                    row,
                    m.key,
                    provenance="unavailable",
                    value_raw=None,
                    value_num=None,
                    value_text=None,
                )
                plan.skipped.append(
                    _skip(
                        field_ref,
                        "not_published",
                        extra=f"{m.key} im Manual nicht angegeben",
                    )
                )
                if write is not None:
                    plan.writes.append(write)
                continue

            if status == "druckfehler_vermutet":
                write = _metric_write(
                    row,
                    m.key,
                    provenance="blank",
                    value_raw=None,
                    value_num=None,
                    value_text=None,
                )
                plan.skipped.append(_skip(field_ref, "cell_misprint"))
                if write is not None:
                    plan.writes.append(write)
                continue

            literal_str = str(literal)
            detail = (
                f"{m.key} {literal_str} aus Normtabelle {group} (RW(ges) {base_value})."
            )
            if m.value_type == "text":
                write = _metric_write(
                    row,
                    m.key,
                    provenance="computed",
                    value_raw=literal_str,
                    value_num=None,
                    value_text=literal_str,
                    detail=detail + unsicher_suffix,
                )
            else:
                numeric = coerce_float(literal_str)
                if numeric is not None:
                    write = _metric_write(
                        row,
                        m.key,
                        provenance="computed",
                        value_raw=literal_str,
                        value_num=numeric,
                        value_text=None,
                        detail=detail + unsicher_suffix,
                    )
                else:
                    write = _metric_write(
                        row,
                        m.key,
                        provenance="computed",
                        value_raw=literal_str,
                        value_num=None,
                        value_text=literal_str,
                        detail=detail + " Wie im Manual gedruckt." + unsicher_suffix,
                    )
            if write is not None:
                plan.writes.append(write)

    return plan


def run_completion(
    assessment_id: int, *, lookup: LookupFn = _DEFAULT_LOOKUP
) -> CompletionPlan:
    """Read the current DB state for `assessment_id`, plan, and persist.
    Best-effort by design of the caller (pipeline/app wrap this in
    try/except); this function itself lets exceptions propagate.
    """
    assessment = store.get_assessment(assessment_id)
    if assessment is None:
        return CompletionPlan()
    metrics_rows = store.get_assessment_metrics(assessment_id)
    subtest_rows = store.get_subtest_scores(assessment_id)
    definition = store.get_test_definition(assessment.get("test_version"))
    plan = plan_completion(
        assessment, metrics_rows, subtest_rows, definition, lookup=lookup
    )
    store.apply_norm_completion(assessment_id, plan)
    return plan
