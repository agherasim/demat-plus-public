"""`demat.pipeline._process_sheet` runs norm completion in its own
try/except, immediately after `save_assessment` and before
`run_review_validations` (ADR 0013). A dummy `SkillRunner` is enough here —
`extract_sheet` and `run_review_validations` are both monkeypatched, so
nothing calls a real agent.
"""

from __future__ import annotations

import pytest

from demat import config, norm_completion, store
from demat.extraction import ExtractionResult
from demat.pdf_render import PageImage
from demat.pipeline import _process_sheet
from demat.review_validation import ReviewValidationResult
from demat.schema import ExtractedSheet, SubtestScore, TriageResult

TEST_SALT = "test-salt"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    store.init_db()
    return tmp_path


class _DummyRunner:
    def run(self, *a, **k):
        raise AssertionError("must not be called: extract_sheet is monkeypatched")

    def run_text(self, *a, **k):
        raise AssertionError("must not be called: run_review_validations is stubbed")


def _sheet(**overrides) -> ExtractedSheet:
    defaults = {
        "is_demat_sheet": True,
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "child_name": "Max Mustermann",
        "child_name_confidence": "high",
        "klasse": "3c",
        "gender": "junge",
        "test_date_raw": "16.09.26",
        "test_date_iso": "2026-09-16",
        "test_date_confidence": "high",
        "subtests": [
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ],
        "overall": [],
        "assessment_note": None,
        "illegible_notes": None,
        "overall_confidence": "high",
    }
    defaults.update(overrides)
    return ExtractedSheet(**defaults)


def _triage() -> TriageResult:
    return TriageResult(
        is_demat_sheet=True, test_version="DEMAT 2+", test_form="A", confidence="high"
    )


def _patch_extract_sheet(monkeypatch, sheet: ExtractedSheet, norm_variant=None):
    import demat.pipeline as pipeline_module

    store.upsert_source_file("file-sha-1", "test.pdf", 1)
    definition = store.get_test_definition(sheet.test_version)

    def fake_extract_sheet(runner, image_path, output_path_base, stop_event=None):
        return ExtractionResult(
            status="ok",
            triage=_triage(),
            extracted=sheet,
            skill_used="demat-2plus-extraction",
            raw_response={"is_demat_sheet": True},
            unknown_version=False,
            definition=definition,
        )

    monkeypatch.setattr(pipeline_module, "extract_sheet", fake_extract_sheet)
    return pipeline_module


def _page(tmp_path, key="page-key-1") -> PageImage:
    return PageImage(page_index=0, path=str(tmp_path / "page.png"), page_key=key)


def test_completion_runs_before_review_validation(isolated_db, monkeypatch):
    pipeline_module = _patch_extract_sheet(monkeypatch, _sheet())

    order: list[str] = []
    real_run_completion = norm_completion.run_completion

    def spy_run_completion(assessment_id, **kwargs):
        order.append("completion")
        return real_run_completion(assessment_id, **kwargs)

    def spy_run_review_validations(*args, **kwargs):
        order.append("validation")
        return ReviewValidationResult(findings=[])

    monkeypatch.setattr(
        pipeline_module.norm_completion, "run_completion", spy_run_completion
    )
    monkeypatch.setattr(
        pipeline_module, "run_review_validations", spy_run_review_validations
    )

    result = _process_sheet(
        _page(isolated_db), "file-sha-1", _DummyRunner(), TEST_SALT, {}
    )

    assert result.status == "saved"
    assert order == ["completion", "validation"]


def test_completion_exception_does_not_lose_the_saved_row(isolated_db, monkeypatch):
    pipeline_module = _patch_extract_sheet(monkeypatch, _sheet())

    def raising_run_completion(assessment_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        pipeline_module.norm_completion, "run_completion", raising_run_completion
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_review_validations",
        lambda *a, **k: ReviewValidationResult(findings=[]),
    )

    result = _process_sheet(
        _page(isolated_db), "file-sha-1", _DummyRunner(), TEST_SALT, {}
    )

    assert result.status == "saved"
    # The row exists and validation still ran despite completion raising.
    rows = store.list_inbox_assessments()
    assert len(rows) == 1
    saved = store.get_assessment(rows[0]["assessment_id"])
    assert saved is not None
    assert saved["review_validation_json"] is not None


def test_sheet_result_reflects_norm_values_computed_flag_non_blocking(
    isolated_db, monkeypatch
):
    pipeline_module = _patch_extract_sheet(monkeypatch, _sheet())

    def fake_run_completion(assessment_id, **kwargs):
        # Simulate a real completion write without touching the KB: append
        # the flag directly, the way `apply_norm_completion` would.
        with store.get_connection() as conn:
            conn.execute(
                "UPDATE assessments SET flags = '[\"norm_values_computed\"]' "
                "WHERE assessment_id = ?",
                (assessment_id,),
            )
        return norm_completion.CompletionPlan()

    monkeypatch.setattr(
        pipeline_module.norm_completion, "run_completion", fake_run_completion
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_review_validations",
        lambda *a, **k: ReviewValidationResult(findings=[]),
    )

    result = _process_sheet(
        _page(isolated_db), "file-sha-1", _DummyRunner(), TEST_SALT, {}
    )

    assert result.status == "saved"
    assert "norm_values_computed" in result.flags
    assert result.has_blocking_flags is False
