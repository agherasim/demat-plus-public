"""`process_pdfs`/`process_pdf`: the only module the UI calls directly for
extraction.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from demat import config, identity, norm_completion, norm_variants, pdf_render, store
from demat.agent_runner import (
    ExtractionStoppedError,
    RunnerUnavailableError,
    SkillRunner,
)
from demat.extraction import extract_sheet
from demat.pdf_render import PageImage, render_pages
from demat.review_validation import findings_to_json, run_review_validations
from demat.segmentation import segment_pages
from demat.validation import is_blocking_flag, validate

ProgressCallback = Callable[[int, int, str], None]
# (done, total, current_old_assessment_id)
ReextractProgressCallback = Callable[[int, int, int], None]

logger = logging.getLogger("demat.pipeline")


@dataclass
class SheetResult:
    page_index: int
    status: Literal["saved", "skipped_duplicate", "skipped_not_sheet", "error"]
    person_id: str | None
    test_version: str | None
    skill_used: str | None
    has_blocking_flags: bool = False
    flags: list[str] = field(default_factory=list)
    error: str | None = None
    assessment_id: int | None = None


@dataclass(frozen=True)
class ReplaceTarget:
    """The existing row a re-extract replaces (ADR 0016), and the only state
    carried over from it."""

    assessment_id: int  # row to replace (atomically, inside save_assessment)
    person_id: str | None  # old link; kept if non-null and the persons row exists
    norm_variant: str | None  # old per-assessment Normtabelle (ADR 0010)
    audit_note: str  # appended to the new row's review_notes


ReextractStatus = Literal[
    "replaced",  # new row saved; old row gone
    "kept_not_a_sheet",  # triage said not a sheet; old row untouched
    "kept_error",  # extraction/save failed; old row untouched
    "skipped_missing",  # old row no longer exists
    "skipped_source_unavailable",  # no usable PDF/PNG (or NULL source sha)
    "stopped",  # stop requested before/while processing; untouched
]


@dataclass
class ReextractResult:
    old_assessment_id: int
    status: ReextractStatus
    new_assessment_id: int | None = None
    person_id: str | None = None
    flags: list[str] = field(default_factory=list)
    has_blocking_flags: bool = False
    error: str | None = None


@dataclass
class PdfResult:
    file_name: str
    file_sha256: str
    sheets: list[SheetResult] = field(default_factory=list)


@dataclass
class PdfInput:
    file_name: str
    pdf_bytes: bytes
    file_sha256: str


class BatchIdentityTracker:
    """Guards against silently merging two different children into one
    `person_id` within a single batch (one `process_pdf`/`process_pdfs`
    call). `person_id` is a pure function of the normalized name, so two
    different sheets in the same batch with the literal same name always
    hash to the same id — almost never a legitimate retest, since a
    repeated physical page is already deduped earlier by `page_key`. So a
    repeat within a batch is treated as two different, same-named children:
    the first sheet keeps the deterministic id/key, every later occurrence
    gets `identity.disambiguated_id_and_key`'s character-substitution
    instead, and a warning is logged — this never blocks the batch. Shared
    across concurrent workers in `process_pdfs`, hence the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._assigned: dict[str, str] = {}
        self._used_ids: set[str] = set()

    def resolve(
        self, computed_person_id: str, normalization_key: str
    ) -> tuple[str, str]:
        with self._lock:
            first_id = self._assigned.get(normalization_key)
            if first_id is None:
                self._assigned[normalization_key] = computed_person_id
                self._used_ids.add(computed_person_id)
                return computed_person_id, normalization_key
            new_id, new_key = identity.disambiguated_id_and_key(
                computed_person_id, normalization_key, exclude=self._used_ids
            )
            self._used_ids.add(new_id)
            logger.warning(
                "batch identity guardrail: a name was seen twice in one "
                "batch (person_id=%s already assigned to it this batch) — "
                "assigning a disambiguated person_id=%s instead of merging "
                "into one child",
                first_id,
                new_id,
            )
            return new_id, new_key


def _process_sheet(
    page: PageImage,
    file_sha256: str,
    runner: SkillRunner,
    salt: str,
    norm_variants_by_file: dict[str, str],
    stop_event: threading.Event | None = None,
    identity_tracker: BatchIdentityTracker | None = None,
    *,
    replace: ReplaceTarget | None = None,
) -> SheetResult:
    """Extract, validate and persist the one sheet on `page`.

    Runs standalone (no progress reporting) so it can be scheduled on a
    worker thread by `process_pdfs`; `process_pdf` calls it in a plain loop.

    `norm_variants_by_file` maps `file_sha256` -> the Normtabelle variant
    key chosen for that uploaded PDF at upload time (ADR 0010; one selector
    per file, not per `norm_test` — a batch can mix variants of the same
    test across files). Applied only when it's valid for the sheet's
    *actual* resolved `norm_test` (a PDF's contents can turn out to be a
    different test than the uploader expected); a mismatch is silently left
    unset rather than stamping a variant onto the wrong test, and is
    correctable per-assessment in review.

    `replace` (ADR 0016 re-extract): skip the already-extracted check,
    carry `replace.norm_variant` (if valid for the new `norm_test`, else
    unset with a note), keep `replace.person_id` instead of consulting the
    tracker, and have `save_assessment` delete the old row and insert the
    new one in a single transaction.
    """
    page_index = page.page_index

    if replace is None and store.page_already_extracted(page.page_key):
        return SheetResult(
            page_index=page_index,
            status="skipped_duplicate",
            person_id=None,
            test_version=None,
            skill_used=None,
        )

    try:
        output_base = str(config.RUNS_DIR / page.page_key)
        extraction = extract_sheet(
            runner, page.path, output_base, stop_event=stop_event
        )

        if extraction.status == "not_a_sheet":
            return SheetResult(
                page_index=page_index,
                status="skipped_not_sheet",
                person_id=None,
                test_version=None,
                skill_used=None,
            )

        if extraction.status == "error":
            return SheetResult(
                page_index=page_index,
                status="error",
                person_id=None,
                test_version=extraction.triage.test_version
                if extraction.triage
                else None,
                skill_used=extraction.skill_used,
                error=extraction.error,
            )

        extracted = extraction.extracted
        triage = extraction.triage
        assert extracted is not None and triage is not None

        definition = extraction.definition
        validation = validate(extracted, triage, definition)
        if extraction.unknown_version:
            validation.flags.append("unknown_test_version")
            note = triage.notes or ""
            validation.review_notes = (
                f"{validation.review_notes}; unknown_test_version: {note}"
                if validation.review_notes
                else f"unknown_test_version: {note}"
            )
            validation.has_blocking_flags = True

        computed_person_id = None
        normalization_key = None
        if extracted.child_name:
            computed_person_id, normalization_key = identity.person_id_for(
                extracted.child_name, salt
            )
            # A kept link (re-extract) never consults the tracker: the
            # person is fixed by the source page, not by the name read.
            keeps_link = replace is not None and replace.person_id is not None
            if normalization_key and not keeps_link:
                tracker = identity_tracker or BatchIdentityTracker()
                computed_person_id, normalization_key = tracker.resolve(
                    computed_person_id, normalization_key
                )

        norm_variant = None
        extra_review_note: str | None = None
        if replace is None:
            if definition is not None and definition.norm_test:
                chosen = norm_variants_by_file.get(file_sha256)
                if chosen is not None and norm_variants.is_valid(
                    definition.norm_test, chosen
                ):
                    norm_variant = chosen
        else:
            extra_review_note = replace.audit_note
            carried = replace.norm_variant
            norm_test = definition.norm_test if definition is not None else None
            if carried is not None:
                if norm_test and norm_variants.is_valid(norm_test, carried):
                    norm_variant = carried
                else:
                    extra_review_note += (
                        f"; norm_variant {carried!r} not carried over: "
                        f"not valid for {norm_test!r}"
                    )

        runner_name = "direct" if config.RUNNER == "direct" else "agent_sdk"
        save_result = store.save_assessment(
            file_sha256=file_sha256,
            page_index=page_index,
            page_key=page.page_key,
            page_image_path=page.path,
            extracted=extracted,
            triage=triage,
            extraction_skill=extraction.skill_used or "",
            validation=validation,
            definition=definition,
            model_id=config.MODEL_ID,
            runner=runner_name,
            raw_response=extraction.raw_response or {},
            computed_person_id=computed_person_id,
            normalization_key=normalization_key,
            raw_name=extracted.child_name,
            norm_variant=norm_variant,
            replaces_assessment_id=replace.assessment_id if replace else None,
            keep_person_id=replace.person_id if replace else None,
            extra_review_note=extra_review_note,
        )

        assessment_id = save_result["assessment_id"]
        try:
            # ADR 0013: norm lookup runs immediately before review
            # validation, on the just-saved (`needs_review`) row, so
            # `norm_completion`'s findings see the looked-up values on this
            # same pass. Best-effort: never loses the already-saved
            # assessment, and never stamps `edited_at` (I6) — a system
            # lookup is not a human edit.
            norm_completion.run_completion(assessment_id)
        except Exception:
            logger.exception("norm completion failed for assessment %s", assessment_id)

        try:
            saved_assessment = store.get_assessment(assessment_id)
            assert saved_assessment is not None
            validation_result = run_review_validations(
                saved_assessment,
                store.get_assessment_metrics(assessment_id),
                store.get_subtest_scores(assessment_id),
                definition,
                runner,
                stop_event=stop_event,
            )
            store.save_review_validation(
                assessment_id,
                findings_to_json(validation_result.findings),
                saved_assessment.get("edited_at"),
            )
        except Exception:
            # Best-effort: a failure here must never lose the already-
            # saved assessment. It simply stays unvalidated (reviewers
            # can still run it manually) — see the Assessment Review
            # panel's "Run validation" button.
            logger.exception(
                "review validation failed for assessment %s", assessment_id
            )

        # Re-read: `norm_completion` may have added/removed
        # `norm_values_computed` after `save_assessment` computed
        # `save_result["flags"]`/`has_blocking_flags`.
        final_flags = list(save_result["flags"])
        final_has_blocking = save_result["has_blocking_flags"]
        refreshed = store.get_assessment(assessment_id)
        if refreshed is not None:
            final_flags = refreshed.get("flags", final_flags)
            final_has_blocking = any(is_blocking_flag(f) for f in final_flags)

        return SheetResult(
            page_index=page_index,
            status="saved",
            person_id=save_result["person_id"],
            test_version=extracted.test_version,
            skill_used=extraction.skill_used,
            has_blocking_flags=final_has_blocking,
            flags=final_flags,
            assessment_id=assessment_id,
        )
    except RunnerUnavailableError:
        raise
    except ExtractionStoppedError:
        raise
    except Exception as exc:
        return SheetResult(
            page_index=page_index,
            status="error",
            person_id=None,
            test_version=None,
            skill_used=None,
            error=str(exc),
        )


def process_pdf(
    file_name: str,
    pdf_bytes: bytes,
    file_sha256: str,
    runner: SkillRunner,
    progress_cb: ProgressCallback | None = None,
    norm_variants_by_file: dict[str, str] | None = None,
) -> PdfResult:
    """Render, segment, extract, validate and persist every sheet in one PDF,
    sequentially, reporting progress via `progress_cb` after each sheet.

    `file_sha256` is the already-computed hash of `pdf_bytes` (the caller
    computes it once to also call `store.save_uploaded_document`, ADR 0007).

    One sheet failing (agent error, missing CLI, validation crash) does not
    abort the rest of the PDF. For concurrent extraction across multiple
    sheets/PDFs, see `process_pdfs`.
    """
    config.ensure_data_dirs()
    norm_variants_by_file = norm_variants_by_file or {}

    pages = render_pages(pdf_bytes, file_sha256)
    sheets = segment_pages(pages)
    store.upsert_source_file(file_sha256, file_name, len(pages))

    result = PdfResult(file_name=file_name, file_sha256=file_sha256)
    salt = config.resolve_id_salt()
    identity_tracker = BatchIdentityTracker()
    total = len(sheets)

    for i, sheet in enumerate(sheets, start=1):
        page = pages[sheet.page_indices[0]]
        if progress_cb:
            progress_cb(i, total, "checking for existing extraction")
        sheet_result = _process_sheet(
            page,
            file_sha256,
            runner,
            salt,
            norm_variants_by_file,
            identity_tracker=identity_tracker,
        )
        if progress_cb:
            progress_cb(i, total, sheet_result.skill_used or "triage")
        result.sheets.append(sheet_result)

    return result


def process_pdfs(
    inputs: list[PdfInput],
    runner: SkillRunner,
    progress_cb: ProgressCallback | None = None,
    max_workers: int | None = None,
    norm_variants_by_file: dict[str, str] | None = None,
    stop_event: threading.Event | None = None,
) -> list[PdfResult]:
    """Render, segment, extract, validate and persist every sheet across all
    `inputs`, running sheet extraction concurrently — for pages within one
    PDF and across the several uploaded PDFs alike — bounded by
    `max_workers` (default `config.MAX_PARALLEL_WORKERS`).

    `progress_cb` is called from the calling thread only, after each sheet
    completes, as `(done, total, label)` counted across all inputs combined
    (ordering across files/pages is not preserved).

    A `RunnerUnavailableError` from any sheet aborts the whole batch (matches
    `process_pdf`'s single-PDF behavior): pending work is cancelled and the
    exception re-raised from the calling thread.

    `stop_event`, if given and set (e.g. by a UI "Stop" button, from a
    different thread) while the batch is running: cancels every sheet not
    yet started, and — for a sheet already mid skill-run — the runner
    itself polls `stop_event` and kills its in-flight `claude` CLI call
    (`agent_runner.ExtractionStoppedError`; see `SkillRunner.run`). Either
    way the sheet is simply never extracted; `store.page_already_extracted`
    picks it straight back up on the next run of the same upload (ADR 0007).
    """
    config.ensure_data_dirs()
    max_workers = max_workers or config.MAX_PARALLEL_WORKERS
    salt = config.resolve_id_salt()
    identity_tracker = BatchIdentityTracker()
    norm_variants_by_file = norm_variants_by_file or {}

    results: dict[str, PdfResult] = {}
    pages_by_key: dict[str, PageImage] = {}
    work: list[tuple[str, PageImage]] = []
    seen_page_keys: set[str] = set()

    for pdf_input in inputs:
        pages = render_pages(pdf_input.pdf_bytes, pdf_input.file_sha256)
        sheets = segment_pages(pages)
        store.upsert_source_file(pdf_input.file_sha256, pdf_input.file_name, len(pages))
        results[pdf_input.file_sha256] = PdfResult(
            file_name=pdf_input.file_name, file_sha256=pdf_input.file_sha256
        )
        for sheet in sheets:
            page = pages[sheet.page_indices[0]]
            pages_by_key[page.page_key] = page
            # A page_key repeated within this batch (e.g. the same PDF
            # picked twice in one upload) must only be submitted once —
            # `store.page_already_extracted` is check-then-act and two
            # concurrent workers on the same key would both insert.
            if page.page_key in seen_page_keys:
                continue
            seen_page_keys.add(page.page_key)
            work.append((pdf_input.file_sha256, page))

    total = len(work)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_sheet,
                page,
                file_sha256,
                runner,
                salt,
                norm_variants_by_file,
                stop_event,
                identity_tracker,
            ): (
                file_sha256,
                page,
            )
            for file_sha256, page in work
        }
        stopped = False
        for future in as_completed(futures):
            if future.cancelled():
                continue
            exc = future.exception()
            if isinstance(exc, RunnerUnavailableError):
                for other in futures:
                    other.cancel()
                raise exc
            if isinstance(exc, ExtractionStoppedError):
                # Interrupted mid skill-run by the stop button — not a
                # result for this sheet, and not a failure worth counting.
                continue
            file_sha256, page = futures[future]
            sheet_result = future.result()
            results[file_sha256].sheets.append(sheet_result)
            done += 1
            if progress_cb:
                progress_cb(done, total, sheet_result.skill_used or "triage")
            if not stopped and stop_event is not None and stop_event.is_set():
                stopped = True
                for other in futures:
                    if not other.done():
                        other.cancel()

    for pdf_result in results.values():
        pdf_result.sheets.sort(key=lambda s: s.page_index)

    return [results[pdf_input.file_sha256] for pdf_input in inputs]


def _resolve_reextract_page(
    target: dict, reasons: list[str] | None = None
) -> PageImage | None:
    """The page image to re-extract `target` from (ADR 0016 §9), in order:
    the stored PDF, then `UPLOADS_DIR/<sha>.pdf` (both sha256-verified, the
    single page re-rendered), then the existing page PNG. None if nothing
    verifiable is available — including a NULL `source_file_sha256`, which
    can't be verified at all (never guess).

    Never raises: every failure is logged and treated as "this candidate
    failed". `reasons`, if given, collects one line per failed candidate.
    """
    reasons = reasons if reasons is not None else []
    try:
        sha = target.get("source_file_sha256")
        index = target.get("source_page_index")
        page_key = target.get("source_page_key")
        if not sha or index is None:
            reasons.append("source file sha256 is not recorded")
            return None
        if page_key != pdf_render._page_key(sha, index):
            logger.warning(
                "re-extract: assessment %s page key does not match its source "
                "sha/page index",
                target.get("assessment_id"),
            )
            reasons.append("page key does not match source sha/page index")
            return None

        candidates: list[Path] = []
        if target.get("stored_pdf_path"):
            candidates.append(Path(target["stored_pdf_path"]))
        uploads_copy = config.UPLOADS_DIR / f"{sha}.pdf"
        if uploads_copy not in candidates:
            candidates.append(uploads_copy)

        for candidate in candidates:
            try:
                if not candidate.exists():
                    reasons.append(f"{candidate.name}: not found")
                    continue
                pdf_bytes = candidate.read_bytes()
                if hashlib.sha256(pdf_bytes).hexdigest() != sha:
                    logger.warning(
                        "re-extract: %s does not match source sha256; skipped",
                        candidate,
                    )
                    reasons.append(f"{candidate.name}: sha256 mismatch")
                    continue
                return pdf_render.render_page(pdf_bytes, sha, index)
            except IndexError:
                logger.warning(
                    "re-extract: page index %s out of range for %s", index, candidate
                )
                reasons.append(f"{candidate.name}: page index {index} out of range")
            except Exception:
                logger.exception("re-extract: rendering %s failed", candidate)
                reasons.append(f"{candidate.name}: could not be rendered")

        png_candidates: list[Path] = []
        if target.get("page_image_path"):
            png_candidates.append(Path(target["page_image_path"]))
        png_candidates.append(config.PAGES_DIR / f"{page_key}.png")
        for png in png_candidates:
            if png.exists():
                return PageImage(page_index=index, page_key=page_key, path=str(png))
        reasons.append("page image not found")
        return None
    except Exception:
        logger.exception(
            "re-extract: resolving the source page for assessment %s failed",
            target.get("assessment_id"),
        )
        reasons.append("resolving the source page failed")
        return None


def _reextract_audit_note(target: dict) -> str:
    note = f"re-extracted from assessment #{target['assessment_id']} (was {target['status']}"
    if target.get("edited_at"):
        note += ", edited"
    if target.get("finalized_at"):
        note += f", finalized {target['finalized_at']}"
    return note + "); prior manual edits and review findings were not carried over"


def reextract_assessments(
    assessment_ids: list[int],
    runner: SkillRunner,
    progress_cb: ReextractProgressCallback | None = None,
    stop_event: threading.Event | None = None,
    results_sink: list[ReextractResult] | None = None,
) -> list[ReextractResult]:
    """Re-read each assessment's source page and replace its row with a
    fresh extraction (ADR 0016), sequentially, one row at a time.

    Only `person_id` and a still-valid `norm_variant` carry over; a row
    whose extraction fails (or is stopped, or has no usable source) is left
    untouched.

    Results in input order. Each result is also appended to `results_sink`
    (if given) the moment its row finishes, so a caller still sees what was
    done when the batch aborts. Raises RunnerUnavailableError (batch aborted;
    rows already replaced stay replaced, the rest untouched).
    """
    config.ensure_data_dirs()
    salt = config.resolve_id_salt()
    tracker = BatchIdentityTracker()
    ids = list(dict.fromkeys(assessment_ids))
    total = len(ids)
    results: list[ReextractResult] = []

    def _finish(result: ReextractResult) -> None:
        results.append(result)
        if results_sink is not None:
            results_sink.append(result)
        if progress_cb:
            progress_cb(len(results), total, result.old_assessment_id)

    for position, aid in enumerate(ids):
        if stop_event is not None and stop_event.is_set():
            _finish(ReextractResult(old_assessment_id=aid, status="stopped"))
            continue

        target = store.get_reextract_target(aid)
        if target is None:
            _finish(ReextractResult(old_assessment_id=aid, status="skipped_missing"))
            continue

        reasons: list[str] = []
        page = _resolve_reextract_page(target, reasons)
        if page is None:
            _finish(
                ReextractResult(
                    old_assessment_id=aid,
                    status="skipped_source_unavailable",
                    error="; ".join(reasons) or "no usable source",
                )
            )
            continue

        # ADR 0016 §3: a run that writes nothing must fail, not re-read the
        # previous extraction's file at the same path.
        store.clear_run_outputs(target["source_page_key"])

        replace_target = ReplaceTarget(
            assessment_id=aid,
            person_id=target["person_id"],
            norm_variant=target["norm_variant"],
            audit_note=_reextract_audit_note(target),
        )
        try:
            sheet = _process_sheet(
                page,
                target["source_file_sha256"],
                runner,
                salt,
                {},
                stop_event,
                tracker,
                replace=replace_target,
            )
        except ExtractionStoppedError:
            for remaining in ids[position:]:
                _finish(ReextractResult(old_assessment_id=remaining, status="stopped"))
            break

        if sheet.status == "saved":
            result = ReextractResult(
                old_assessment_id=aid,
                status="replaced",
                new_assessment_id=sheet.assessment_id,
                person_id=sheet.person_id,
                flags=list(sheet.flags),
                has_blocking_flags=sheet.has_blocking_flags,
            )
            store.delete_review_validation_run_dir(aid)
        elif sheet.status == "skipped_not_sheet":
            result = ReextractResult(old_assessment_id=aid, status="kept_not_a_sheet")
        else:
            result = ReextractResult(
                old_assessment_id=aid,
                status="kept_error",
                error=sheet.error or f"unexpected result {sheet.status!r}",
            )
        _finish(result)

    return results
