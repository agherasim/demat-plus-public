"""Class detection, grouping and staleness (ADR 0008, spec §4).

Pure functions over `store` results; no Streamlit. `list_groups()` is the
page's single entry point for the left-hand class list.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from demat import store


class ClassGroup(BaseModel):
    klasse: str
    schuljahr: str
    test_version: str
    assessment_ids: list[int]
    child_count: int
    awaiting_review_count: int
    test_forms: list[str]
    test_date_min: str | None
    test_date_max: str | None
    norm_variant: str | None = None  # unanimous asserted variant, ADR 0010
    norm_variants: list[str] = []  # distinct non-NULL variants; NULLs abstain
    state: str  # "new" | "generated" | "stale"
    latest_profile: dict | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.klasse, self.schuljahr, self.test_version)

    @property
    def test_form_summary(self) -> str | None:
        """'A' | 'B' | 'A/B' from the distinct test_form values in the
        group (spec §4.2). `test_form` is not part of the grouping key.
        """
        if not self.test_forms:
            return None
        if len(self.test_forms) == 1:
            return self.test_forms[0]
        return "/".join(self.test_forms)


def content_hash(assessment_rows: list[dict]) -> str:
    """sha256 over the sorted (assessment_id, edited_at, finalized_at)
    tuples (spec §4.4's opt-in staleness sensitivity, OQ-A). Detects an
    edit to an already-included, already-validated assessment — the
    id-set comparison alone would miss that.
    """
    tuples = sorted(
        (r["assessment_id"], r.get("edited_at"), r.get("finalized_at"))
        for r in assessment_rows
    )
    payload = json.dumps(tuples, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state_for(
    assessment_ids: list[int],
    latest_profile: dict | None,
    current_content_hash: str | None,
) -> str:
    """new / generated / stale (spec §4.4). Set comparison, not a count and
    not a timestamp comparison. `content_hash`, when both sides have one, is
    an additional (OQ-A-decided) sensitivity increase: a post-validation
    edit to an already-included row also marks the profile stale.
    """
    if latest_profile is None:
        return "new"
    if set(latest_profile["assessment_ids"]) != set(assessment_ids):
        return "stale"
    stored_hash = latest_profile.get("content_hash")
    if stored_hash and current_content_hash and stored_hash != current_content_hash:
        return "stale"
    return "generated"


def list_groups() -> list[ClassGroup]:
    """Every class with >=1 validated assessment (spec §4.3's "listed"
    rule), with its badge state resolved against the latest
    `class_profiles` row for that key. Sort order: `schuljahr DESC, klasse
    ASC, test_version ASC` (spec §4.4).
    """
    raw_groups = store.list_class_groups()
    result: list[ClassGroup] = []
    for g in raw_groups:
        if g["child_count"] == 0:
            continue
        latest_profile = store.latest_class_profile(
            g["klasse"], g["schuljahr"], g["test_version"]
        )
        current_hash = None
        if latest_profile is not None and latest_profile.get("content_hash"):
            rows = store.get_class_assessments(
                g["klasse"], g["schuljahr"], g["test_version"]
            )
            current_hash = content_hash(rows)
        state = _state_for(g["assessment_ids"], latest_profile, current_hash)
        result.append(
            ClassGroup(
                klasse=g["klasse"],
                schuljahr=g["schuljahr"],
                test_version=g["test_version"],
                assessment_ids=g["assessment_ids"],
                child_count=g["child_count"],
                awaiting_review_count=g["awaiting_review_count"],
                test_forms=g["test_forms"],
                test_date_min=g["test_date_min"],
                test_date_max=g["test_date_max"],
                norm_variant=g.get("norm_variant"),
                norm_variants=g.get("norm_variants", []),
                state=state,
                latest_profile=latest_profile,
            )
        )
    # schuljahr DESC, klasse ASC, test_version ASC: two stable sorts, outer
    # key applied last, so ties within a schuljahr keep klasse/test_version order.
    result.sort(key=lambda g: (g.klasse, g.test_version))
    result.sort(key=lambda g: g.schuljahr, reverse=True)
    return result
