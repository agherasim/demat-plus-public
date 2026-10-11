"""Stage 4 — prose (ADR 0008 spec §6.4). The only non-deterministic stage;
receives stage 2+3's output as structured input and may not compute,
restate, or invent any number. Enforcement is a validator, not a prompt
instruction: `ProseValidationError` on a violation, one automatic retry,
then a visible failure. Never saves unvalidated prose.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from demat.agent_runner import SkillRunner, SkillRunnerError
from demat.reports.data import ClassProfileData

PROSE_SKILL = "demat-class-profile"

# A small whitelist beyond the input JSON's own numbers: 1..10 ordinals,
# 100, 50, and the year — spec §6.4's numeric-containment allowance.
_WHITELIST_NUMBERS = {str(n) for n in range(1, 11)} | {"100", "50"}

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

_DIAGNOSIS_TERMS = re.compile(
    r"Rechenschwäche|Rechenstörung|Dyskalkulie|Diagnose|diagnostiziert",
    re.IGNORECASE,
)
_REFERRAL_TERMS = re.compile(
    r"Förderdiagnostik|weitere Abklärung|abklären|erwägen|ausschließen",
    re.IGNORECASE,
)
_ASSERTIVE_TERMS = re.compile(r"\bhat\s+eine\b|liegt\s+eine\b.*\bvor\b", re.IGNORECASE)


class ProseValidationError(Exception):
    """Raised when generated prose fails a post-generation validator
    (numeric containment, the diagnosis check, or coverage). A prompt
    instruction is not an enforcement mechanism; this is.
    """


class ChildProse(BaseModel):
    person_ref: str
    staerken: str
    schwaechen: str = ""
    empfehlungen: list[str] = Field(default_factory=list)


class ClassProse(BaseModel):
    profil_staerken: str
    profil_schwaechen: str
    profil_muster: str
    empfehlungen: list[str] = Field(default_factory=list)
    kinder: list[ChildProse] = Field(default_factory=list)


def _allowed_numbers(input_json_text: str) -> set[str]:
    """Every integer/decimal token in the serialised input JSON, plus the
    year appearing in any test date, normalised (German comma -> dot,
    trailing zeros stripped) for membership testing.
    """
    tokens = set()
    for match in _NUMBER_RE.findall(input_json_text):
        normalized = match.replace(",", ".")
        tokens.add(normalized)
        try:
            as_float = float(normalized)
            if as_float == int(as_float):
                tokens.add(str(int(as_float)))
        except ValueError:
            pass
    return tokens | _WHITELIST_NUMBERS


def _tokens_in_text(text: str) -> list[str]:
    return [m.replace(",", ".") for m in _NUMBER_RE.findall(text)]


def _all_prose_text(prose: ClassProse) -> list[tuple[str, str]]:
    """(field_name, text) pairs to run both validators over."""
    fields = [
        ("profil_staerken", prose.profil_staerken),
        ("profil_schwaechen", prose.profil_schwaechen),
        ("profil_muster", prose.profil_muster),
    ]
    for bullet in prose.empfehlungen:
        fields.append(("empfehlungen", bullet))
    for child in prose.kinder:
        fields.append((f"kinder[{child.person_ref}].staerken", child.staerken))
        fields.append((f"kinder[{child.person_ref}].schwaechen", child.schwaechen))
        for bullet in child.empfehlungen:
            fields.append((f"kinder[{child.person_ref}].empfehlungen", bullet))
    return fields


def validate_numeric_containment(prose: ClassProse, input_json_text: str) -> list[str]:
    """Returns the offending tokens (empty list = passes)."""
    allowed = _allowed_numbers(input_json_text)
    offending = []
    for _field, text in _all_prose_text(prose):
        for token in _tokens_in_text(text):
            if token not in allowed:
                offending.append(token)
    return offending


def validate_diagnosis_terms(prose: ClassProse) -> list[str]:
    """Returns the offending sentences (empty list = passes). The bare
    assertive form of a diagnosis term is banned; the term is permitted
    only inside a referral construction (same sentence as one of
    Förderdiagnostik/weitere Abklärung/abklären/erwägen/ausschließen) and
    never adjacent to `hat eine` / `liegt eine ... vor`.
    """
    offending = []
    for _field, text in _all_prose_text(prose):
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if not _DIAGNOSIS_TERMS.search(sentence):
                continue
            if _ASSERTIVE_TERMS.search(sentence):
                offending.append(sentence)
                continue
            if not _REFERRAL_TERMS.search(sentence):
                offending.append(sentence)
    return offending


def validate_coverage(prose: ClassProse, data: ClassProfileData) -> list[str]:
    """Returns problems (empty list = passes): child count mismatch, or
    any `person_ref` that doesn't resolve to a child in `data`.
    """
    problems = []
    expected_refs = {c.person_ref for c in data.children}
    got_refs = {c.person_ref for c in prose.kinder}
    if len(prose.kinder) != len(data.children):
        problems.append(
            f"expected {len(data.children)} children, got {len(prose.kinder)}"
        )
    missing = expected_refs - got_refs
    if missing:
        problems.append(f"missing person_ref(s): {sorted(missing)}")
    unknown = got_refs - expected_refs
    if unknown:
        problems.append(f"unknown person_ref(s): {sorted(unknown)}")
    return problems


def validate_prose(
    prose: ClassProse, data: ClassProfileData, input_json_text: str
) -> None:
    problems = []
    numeric_offenders = validate_numeric_containment(prose, input_json_text)
    if numeric_offenders:
        problems.append(
            f"numbers not in the input data: {sorted(set(numeric_offenders))}"
        )
    diagnosis_offenders = validate_diagnosis_terms(prose)
    if diagnosis_offenders:
        problems.append(f"bare diagnosis assertions: {diagnosis_offenders}")
    coverage_problems = validate_coverage(prose, data)
    problems.extend(coverage_problems)
    if problems:
        raise ProseValidationError("; ".join(problems))


def _build_prompt(data: ClassProfileData) -> str:
    return (
        "Draft the interpretive sections of a DEMAT Classroom Profile report "
        "in German, following the demat-class-profile skill's rules exactly. "
        "The complete computed class data (every number you may use) is the "
        "attached JSON. Write only the prose fields of the ClassProse schema."
    )


def generate(
    data: ClassProfileData,
    runner: SkillRunner,
    run_dir: Path,
) -> ClassProse:
    """Stage 4: one LLM call producing only interpretive prose, schema-
    constrained and post-validated. One automatic retry on validation
    failure (quoting the offending tokens back), then a visible failure.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "input.json"
    output_path = run_dir / "prose.json"

    input_json_text = data.model_dump_json()
    input_path.write_text(input_json_text, encoding="utf-8")

    last_error: Exception | None = None
    for attempt in range(2):
        prompt = _build_prompt(data)
        if attempt:
            prompt += f"\n\nCorrection needed from a previous attempt: {last_error}"
        try:
            raw: dict[str, Any] = runner.run_text(
                prompt, str(output_path), ClassProse, skill_name=PROSE_SKILL
            )
        except SkillRunnerError as exc:
            last_error = exc
            continue
        try:
            prose = ClassProse.model_validate(raw)
            validate_prose(prose, data, input_json_text)
            return prose
        except Exception as exc:
            last_error = exc
            continue
    raise ProseValidationError(f"prose generation failed after retry: {last_error}")
