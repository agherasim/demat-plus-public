"""`ExtractionStoppedError` (stop_event fired mid skill-run) must propagate
out of `_process_sheet` as an interruption, never get swallowed into a
SheetResult(status="error") the way a real extraction failure would.
"""

from __future__ import annotations

import threading

import pytest

from demat import config, store
from demat.agent_runner import ExtractionStoppedError
from demat.pdf_render import PageImage
from demat.pipeline import _process_sheet


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


class _StoppedRunner:
    """Fake SkillRunner simulating `stop_event` firing mid skill-run."""

    def run(
        self,
        skill_name,
        image_path,
        output_path,
        extra_instructions=None,
        context=None,
        stop_event=None,
    ):
        raise ExtractionStoppedError("stopped by user")

    def run_text(
        self, prompt, output_path, output_model, skill_name=None, stop_event=None
    ):
        raise ExtractionStoppedError("stopped by user")


def test_process_sheet_propagates_extraction_stopped(isolated_db):
    page = PageImage(
        page_index=0, page_key="page-key-1", path=str(isolated_db / "page.png")
    )
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(ExtractionStoppedError):
        _process_sheet(
            page, "file-sha-1", _StoppedRunner(), "salt", {}, stop_event=stop_event
        )

    # A sheet interrupted by a stop is never marked extracted — it must be
    # picked back up the next time the same upload runs (ADR 0007).
    assert not store.page_already_extracted(page.page_key)
