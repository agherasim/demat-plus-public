"""Pydantic models for the extracted DEMAT sheet schema. See spec `Pydantic models`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Confidence = Literal["high", "medium", "low"]
Gender = Literal["junge", "maedchen", "unknown"]

# What the model observed about a norm cell (PR / PR-Band / T / T-Band) the
# sheet leaves for the evaluator to fill in from the manual (ADR 0013). The
# model reports only what it sees — Python decides whether/how to fill it
# (`demat/norm_completion.py`); the model never infers a value from an RW.
CellState = Literal["written", "blank", "illegible"]


class TriageResult(BaseModel):
    is_demat_sheet: bool
    test_version: str | None = None  # "DEMAT 1+" | "DEMAT 2+" | "DEMAT 3+" | "DEMAT 5+"
    test_form: str | None = None  # e.g. "A" from "Testheft Form A"
    confidence: Confidence
    notes: str | None = None


class SubtestScore(BaseModel):
    key: str  # "ZE", "LÄ", "AD", ... as printed (resolved to a canonical key in store.py)
    name: str | None = None  # "Zahleneigenschaften"
    rw: str | None = (
        None  # verbatim as printed, e.g. "3,5" — coerced to REAL in store.py
    )
    rw_max: str | None = None  # verbatim as printed, e.g. "4"
    # What the model observed about the RW cell (ADR 0015). Only matters for
    # a sum-target subtest (DEMAT 3+ ARI/SAR/GEO): an RW left `blank` there
    # is sum-filled by `demat/norm_completion.py`. None = pre-ADR-0015.
    rw_state: CellState | None = None
    pr: int | None = None
    pr_state: CellState | None = None  # None = stub skill / pre-ADR-0013 response
    confidence: Confidence


class MetricValue(BaseModel):
    key: str  # matches a test_definition_metrics.metric_key, e.g. "rw_total"
    value: str | None = None  # verbatim as printed; null if blank/illegible
    value_state: CellState | None = None  # None = stub skill / pre-ADR-0013 response
    confidence: Confidence


class ExtractedSheet(BaseModel):
    is_demat_sheet: bool
    test_version: str | None = None
    test_form: str | None = None
    child_name: str | None = None
    child_name_confidence: Confidence
    klasse: str | None = None  # e.g. "3c"
    gender: Gender
    test_date_raw: str | None = None  # exactly as written, e.g. "16.09.26"
    test_date_iso: str | None = None  # YYYY-MM-DD, or null
    test_date_confidence: Confidence
    subtests: list[SubtestScore] = []  # printed order preserved
    overall: list[
        MetricValue
    ] = []  # generic overall/Gesamtwert block; was OverallScores
    assessment_note: str | None = None  # handwritten free text, verbatim German
    illegible_notes: str | None = None
    overall_confidence: Confidence
