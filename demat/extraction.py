"""Triage -> routing -> extraction -> Pydantic validation -> one corrective retry."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from demat import store, test_registry
from demat.agent_runner import ExtractionStoppedError, SkillRunner, SkillRunnerError
from demat.schema import ExtractedSheet, TriageResult
from demat.test_registry import TestDefinition

TRIAGE_SKILL = "demat-sheet-triage"
FALLBACK_SKILL = "demat-generic-extraction"


@dataclass
class ExtractionResult:
    status: str  # "ok" | "not_a_sheet" | "error"
    triage: TriageResult | None
    extracted: ExtractedSheet | None
    skill_used: str | None
    raw_response: dict[str, Any] | None
    unknown_version: bool
    definition: TestDefinition | None = None
    error: str | None = None


def _run_with_retry[ModelT: BaseModel](
    runner: SkillRunner,
    skill_name: str,
    image_path: str,
    output_path: str,
    model: type[ModelT],
    *,
    context: str | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[ModelT, dict[str, Any]]:
    """Run `skill_name`, validate with `model`, one corrective retry on failure.

    `context` (the definition's expected key/label list, ADR 0005 §4) is held
    constant across attempts and passed separately from `extra_instructions`
    — the two runners word `extra_instructions` as "correction needed from a
    previous attempt", which would be actively wrong on a first attempt.

    `ExtractionStoppedError` (from `stop_event`) is never retried — it means
    the user asked to stop, not that this attempt failed.
    """
    last_error: Exception | None = None
    for attempt in range(2):
        extra = str(last_error) if attempt else None
        try:
            raw = runner.run(
                skill_name,
                image_path,
                output_path,
                extra_instructions=extra,
                context=context,
                stop_event=stop_event,
            )
        except ExtractionStoppedError:
            raise
        except SkillRunnerError as exc:
            last_error = exc
            continue
        try:
            parsed = model.model_validate(raw)
            return parsed, raw
        except ValidationError as exc:
            last_error = exc
            continue
    raise last_error if last_error else SkillRunnerError("unknown extraction failure")


def extract_sheet(
    runner: SkillRunner,
    image_path: str,
    output_path_base: str,
    stop_event: threading.Event | None = None,
) -> ExtractionResult:
    """Run triage then the routed extraction skill for one page image.

    `output_path_base` is used to derive distinct output paths for the
    triage pass and the extraction pass (`<base>.triage.json`,
    `<base>.json`).

    `stop_event` behaves as documented on `agent_runner.SkillRunner.run`;
    an `ExtractionStoppedError` propagates out of this function rather than
    becoming an `ExtractionResult(status="error")` — the caller
    (`pipeline._process_sheet`) treats it as "not extracted", not a failure.
    """
    triage_output = f"{output_path_base}.triage.json"
    extraction_output = f"{output_path_base}.json"

    try:
        triage, _raw_triage = _run_with_retry(
            runner,
            TRIAGE_SKILL,
            image_path,
            triage_output,
            TriageResult,
            stop_event=stop_event,
        )
    except ExtractionStoppedError:
        raise
    except (SkillRunnerError, ValidationError) as exc:
        return ExtractionResult(
            status="error",
            triage=None,
            extracted=None,
            skill_used=TRIAGE_SKILL,
            raw_response=None,
            unknown_version=False,
            error=f"triage failed: {exc}",
        )

    if not triage.is_demat_sheet:
        return ExtractionResult(
            status="not_a_sheet",
            triage=triage,
            extracted=None,
            skill_used=None,
            raw_response=None,
            unknown_version=False,
        )

    with store.get_connection() as conn:
        canonical_version = test_registry.resolve_alias(conn, triage.test_version)
        definition = (
            test_registry.get(conn, canonical_version) if canonical_version else None
        )

    unknown_version = definition is None
    skill_name = definition.extraction_skill if definition else FALLBACK_SKILL
    context = test_registry.prompt_contract(definition)

    try:
        extracted, raw = _run_with_retry(
            runner,
            skill_name,
            image_path,
            extraction_output,
            ExtractedSheet,
            context=context,
            stop_event=stop_event,
        )
    except ExtractionStoppedError:
        raise
    except (SkillRunnerError, ValidationError) as exc:
        return ExtractionResult(
            status="error",
            triage=triage,
            extracted=None,
            skill_used=skill_name,
            raw_response=None,
            unknown_version=unknown_version,
            definition=definition,
            error=f"extraction failed: {exc}",
        )

    return ExtractionResult(
        status="ok",
        triage=triage,
        extracted=extracted,
        skill_used=skill_name,
        raw_response=raw,
        unknown_version=unknown_version,
        definition=definition,
    )
