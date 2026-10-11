"""SQLite persistence. Short-lived connections only (see ADR 0003)."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pypdfium2

from demat import config, identity, norm_variants, test_registry
from demat.schema import ExtractedSheet, TriageResult
from demat.test_registry import TestDefinition
from demat.validation import ValidationResult, coerce_float

if TYPE_CHECKING:
    # Deferred to avoid a circular import — `norm_completion.run_completion`
    # calls `apply_norm_completion` below (ADR 0013).
    from demat.norm_completion import CompletionPlan

_TERMINAL_STATES = {"validated_ok", "validated_with_changes", "rejected"}

# Provenance vocabulary for `subtest_scores.pr_provenance` /
# `assessment_metrics.value_provenance` (ADR 0013). Validated in Python, not
# a `CHECK` constraint, so the columns stay purely additive (ADR 0010).
# NULL on a legacy row reads as 'sheet' — there is no backfill.
PROVENANCE_VALUES = {"sheet", "blank", "computed", "unavailable", "manual"}
# The provenance values a completion run may still write over on a re-run.
COMPLETION_OWNED = {"blank", "computed", "unavailable"}

logger = logging.getLogger("demat.store")

# Whitelisted `assessments` scalar columns editable from the Inbox review
# panel. `person_id` is deliberately absent (read-only, ADR 0007).
_EDITABLE_ASSESSMENT_FIELDS = {
    "klasse",
    "gender",
    "test_version",
    "test_form",
    "test_date_raw",
    "test_date_iso",
    "assessment_note",
    "norm_variant",
}


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    config.ensure_data_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after a database may already have been created by an older
# schema.sql. `CREATE TABLE IF NOT EXISTS` (below) is a no-op on an existing
# table, so these are backfilled additively here instead of requiring the
# destructive `scripts/reset_data.py` for what is otherwise a compatible
# change. (`TEXT` column, `NULL` default only — nothing that could conflict
# with existing data.) Table names come only from this literal.
_ADDITIVE_COLUMNS: dict[str, list[str]] = {
    "assessments": [
        "review_validation_json",
        "review_validation_at",
        "review_validation_edited_at",
        "norm_variant",
    ],
    "subtest_scores": ["pr_provenance", "rw_provenance"],
    "assessment_metrics": ["value_provenance"],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def init_db() -> None:
    config.ensure_data_dirs()
    sql = config.SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(sql)
        _add_missing_columns(conn)
        test_registry.sync_to_db(conn)


def get_test_definition(test_version: str | None) -> TestDefinition | None:
    with get_connection() as conn:
        return test_registry.get(conn, test_version)


def resolve_test_version_alias(raw_version: str | None) -> str | None:
    with get_connection() as conn:
        return test_registry.resolve_alias(conn, raw_version)


def page_already_extracted(page_key: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM assessments WHERE source_page_key = ?", (page_key,)
        ).fetchone()
    return row is not None


def upsert_source_file(file_sha256: str, file_name: str, page_count: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_files (file_sha256, file_name, page_count) "
            "VALUES (?, ?, ?)",
            (file_sha256, file_name, page_count),
        )


def save_uploaded_document(
    file_sha256: str, file_name: str, pdf_bytes: bytes
) -> dict[str, Any]:
    """Persist the uploaded PDF to `UPLOADS_DIR` and record it in
    `source_files`, so the Inbox review panel can offer the original document
    (ADR 0007). Skips the write if the file already exists (same sha256, no
    re-write); never clobbers an existing `stored_pdf_path`
    (`INSERT OR IGNORE`, mirroring `upsert_source_file`).

    Returns {"file_sha256": str, "stored_pdf_path": str, "page_count": int}.
    """
    config.ensure_data_dirs()
    stored_path = config.UPLOADS_DIR / f"{file_sha256}.pdf"
    if not stored_path.exists():
        stored_path.write_bytes(pdf_bytes)

    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page_count = len(pdf)
    finally:
        pdf.close()

    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_files "
            "(file_sha256, file_name, page_count, stored_pdf_path) "
            "VALUES (?, ?, ?, ?)",
            (file_sha256, file_name, page_count, str(stored_path)),
        )

    return {
        "file_sha256": file_sha256,
        "stored_pdf_path": str(stored_path),
        "page_count": page_count,
    }


def _find_person_by_id(conn: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM persons WHERE person_id = ?", (person_id,)
    ).fetchone()


def _most_recent_known_gender(conn: sqlite3.Connection, person_id: str) -> str | None:
    row = conn.execute(
        "SELECT gender FROM assessments WHERE person_id = ? AND gender IS NOT NULL "
        "AND gender != 'unknown' ORDER BY created_at DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    return row["gender"] if row else None


def upsert_person(
    conn: sqlite3.Connection,
    person_id: str,
    normalization_key: str,
) -> None:
    conn.execute(
        "INSERT INTO persons (person_id, normalization_key) VALUES (?, ?) "
        "ON CONFLICT(person_id) DO NOTHING",
        (person_id, normalization_key),
    )


def _resolve_person_link(
    conn: sqlite3.Connection,
    *,
    computed_person_id: str,
    normalization_key: str,
    raw_name: str | None,
    incoming_gender: str | None,
    test_version: str | None,
    test_date_iso: str | None,
) -> tuple[str | None, list[str], str | None]:
    """ADR 0001/0006's person-linkage rules, in one place. Shared by
    `save_assessment`'s initial link and `update_assessment_fields`'s
    first-time allocation (a corrected name giving a previously-unlinked
    assessment its first `person_id`) — those two paths used to duplicate
    this logic and had drifted: the manual-edit path never ran the
    gender-mismatch/duplicate checks below, which is how a real incident (two
    same-named, different-gender children silently merged via the Inbox)
    happened. Factored out so they can't drift apart again.

    Returns `(final_person_id, extra_flags, note)`. Never writes
    `assessments`/`flags`/`review_notes` itself — every flag/note this
    returns is store-side and blocking, same as before; the caller decides
    how to fold it into its own flags/review_notes and how to persist
    `final_person_id` (an INSERT vs. an UPDATE differ between the callers).
    """
    extra_flags: list[str] = []
    note: str | None = None

    existing = _find_person_by_id(conn, computed_person_id)
    if existing is None:
        upsert_person(conn, computed_person_id, normalization_key)
        return computed_person_id, extra_flags, note

    if existing["normalization_key"] != normalization_key:
        # Hash collision: different child, same 4-char ID. Never merge.
        note = (
            f"id_collision: computed_id={computed_person_id} "
            f"colliding_key={existing['normalization_key']!r} "
            f"raw_name={raw_name!r}"
        )
        return None, ["id_collision"], note

    final_person_id = computed_person_id
    extra_flags, note = _person_link_checks(
        conn,
        final_person_id,
        incoming_gender=incoming_gender,
        test_version=test_version,
        test_date_iso=test_date_iso,
    )
    return final_person_id, extra_flags, note


def _person_link_checks(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    incoming_gender: str | None,
    test_version: str | None,
    test_date_iso: str | None,
) -> tuple[list[str], str | None]:
    """ADR 0006 §3 (gender mismatch) and §4 (duplicate date) checks for
    linking an assessment to the existing `person_id`. Shared by
    `_resolve_person_link` and `save_assessment`'s kept-identity re-extract
    path (ADR 0016 §5). Returns `(extra_flags, note)`; every flag is
    store-side and blocking.
    """
    extra_flags: list[str] = []
    note: str | None = None
    final_person_id = person_id

    # ADR 0006 §3: linking to an existing person whose stored and incoming
    # gender are both definite and differ.
    stored_gender = _most_recent_known_gender(conn, final_person_id)
    if (
        stored_gender is not None
        and incoming_gender is not None
        and incoming_gender != "unknown"
        and stored_gender != incoming_gender
    ):
        extra_flags.append("possible_person_merge")
        note = (
            f"possible_person_merge: stored_gender={stored_gender!r} "
            f"incoming_gender={incoming_gender!r}"
        )

    # ADR 0006 §4: same person + test_version + non-null date already stored.
    if test_version and test_date_iso:
        dup = conn.execute(
            "SELECT 1 FROM assessments WHERE person_id = ? AND test_version = ? "
            "AND test_date_iso = ? LIMIT 1",
            (final_person_id, test_version, test_date_iso),
        ).fetchone()
        if dup is not None:
            extra_flags.append("duplicate_suspected")
            dup_note = (
                f"duplicate_suspected: person_id={final_person_id} "
                f"test_version={test_version!r} test_date_iso={test_date_iso!r}"
            )
            note = f"{note}; {dup_note}" if note else dup_note

    return extra_flags, note


def _kept_identity_name_matches(
    conn: sqlite3.Connection, person_id: str, fresh_key: str
) -> bool:
    """ADR 0016 §5: does a re-extracted name (`fresh_key`, already
    normalized) match the kept person — its stored `normalization_key`
    (with the ADR 0006 `#X` disambiguation marker stripped), or any known
    `person_names` spelling (e.g. one `rename_assessment_person` added)?
    """
    accepted: set[str] = set()
    person = _find_person_by_id(conn, person_id)
    if person is not None and person["normalization_key"]:
        accepted.add(person["normalization_key"].split("#", 1)[0])
    for row in conn.execute(
        "SELECT raw_name FROM person_names WHERE person_id = ?", (person_id,)
    ):
        accepted.add(identity.normalization_key(row["raw_name"]))
    return fresh_key in accepted


class ReextractConflictError(ValueError):
    """The assessment a re-extract meant to replace no longer exists, or no
    longer owns its source page (deleted/changed meanwhile, ADR 0016 §2)."""


def save_assessment(
    *,
    file_sha256: str,
    page_index: int,
    page_key: str,
    page_image_path: str,
    extracted: ExtractedSheet,
    triage: TriageResult,
    extraction_skill: str,
    validation: ValidationResult,
    definition: TestDefinition | None,
    model_id: str,
    runner: str,
    raw_response: dict,
    computed_person_id: str | None,
    normalization_key: str | None,
    raw_name: str | None,
    norm_variant: str | None = None,
    replaces_assessment_id: int | None = None,
    keep_person_id: str | None = None,
    extra_review_note: str | None = None,
) -> dict[str, Any]:
    """Insert one assessment row (+ metric/subtest rows), handling ID linkage.

    Every saved assessment starts in `needs_review` (T1, ADR 0007) —
    `validate()` no longer decides a status, only flags/review_notes.

    `norm_variant` (ADR 0010), when given, is validated against the resolved
    `definition.norm_test` (`norm_variants.is_valid`) — a value for the wrong
    test, or an unknown value, raises `ValueError`.

    Re-extract (ADR 0016), all keyword-only and off by default:
    - `replaces_assessment_id`: in the same transaction, first delete that
      row (it must still own `page_key`, else `ReextractConflictError`),
      then insert the new one. Any failure rolls the DELETE back too.
    - `keep_person_id`: link to this existing person instead of resolving
      `computed_person_id`; runs the ADR 0006 §3/§4 checks plus the
      `reextract_name_mismatch` check. Falls back to the computed path
      (with a logged warning) if that `persons` row no longer exists.
    - `extra_review_note`: appended to `review_notes`.

    Returns {"assessment_id": int, "person_id": str | None, "flags": list[str],
    "has_blocking_flags": bool}.
    """
    if norm_variant is not None:
        norm_test = definition.norm_test if definition is not None else None
        if not norm_variants.is_valid(norm_test, norm_variant):
            raise ValueError(
                f"norm_variant {norm_variant!r} is not valid for norm_test "
                f"{norm_test!r} (valid: "
                f"{[v.key for v in norm_variants.variants_for(norm_test)]})"
            )

    flags = list(validation.flags)
    review_notes = validation.review_notes
    # possible_person_merge / duplicate_suspected / id_collision (below) are
    # never warning-only; a store-side append always makes this True too, so
    # it doesn't go stale relative to `flags`.
    has_blocking_flags = validation.has_blocking_flags

    with get_connection() as conn:
        final_person_id: str | None = None

        if replaces_assessment_id is not None:
            # ADR 0016 §2: must precede person resolution, so the §4
            # duplicate check can't match the row being replaced.
            cur = conn.execute(
                "DELETE FROM assessments WHERE assessment_id = ? "
                "AND source_page_key = ?",
                (replaces_assessment_id, page_key),
            )
            if cur.rowcount != 1:
                raise ReextractConflictError(
                    f"assessment {replaces_assessment_id} no longer exists or "
                    f"no longer owns page {page_key}"
                )

        kept_identity = False
        if keep_person_id is not None:
            if _find_person_by_id(conn, keep_person_id) is not None:
                kept_identity = True
                final_person_id = keep_person_id
                extra_flags, note = _person_link_checks(
                    conn,
                    final_person_id,
                    incoming_gender=extracted.gender,
                    test_version=extracted.test_version,
                    test_date_iso=extracted.test_date_iso,
                )
                record_spelling = True
                if normalization_key and not _kept_identity_name_matches(
                    conn, final_person_id, normalization_key
                ):
                    extra_flags.append("reextract_name_mismatch")
                    mismatch_note = (
                        f"reextract_name_mismatch: kept person_id={final_person_id} "
                        "(same source page); the re-read name normalizes to a "
                        "different key"
                    )
                    if config.STORE_RAW_NAMES:
                        mismatch_note += f" raw_name={raw_name!r}"
                    note = f"{note}; {mismatch_note}" if note else mismatch_note
                    record_spelling = False
                if extra_flags:
                    flags.extend(extra_flags)
                    has_blocking_flags = True
                    review_notes = f"{review_notes}; {note}" if review_notes else note
                if record_spelling and config.STORE_RAW_NAMES and raw_name:
                    conn.execute(
                        "INSERT OR IGNORE INTO person_names (person_id, raw_name) "
                        "VALUES (?, ?)",
                        (final_person_id, raw_name),
                    )
            else:
                logger.warning(
                    "re-extract: kept person_id=%s no longer exists; falling "
                    "back to computed identity resolution",
                    keep_person_id,
                )

        if (
            not kept_identity
            and computed_person_id is not None
            and normalization_key is not None
        ):
            final_person_id, extra_flags, note = _resolve_person_link(
                conn,
                computed_person_id=computed_person_id,
                normalization_key=normalization_key,
                raw_name=raw_name,
                incoming_gender=extracted.gender,
                test_version=extracted.test_version,
                test_date_iso=extracted.test_date_iso,
            )
            if extra_flags:
                flags.extend(extra_flags)
                has_blocking_flags = True
                review_notes = f"{review_notes}; {note}" if review_notes else note

            if final_person_id is not None and config.STORE_RAW_NAMES and raw_name:
                conn.execute(
                    "INSERT OR IGNORE INTO person_names (person_id, raw_name) "
                    "VALUES (?, ?)",
                    (final_person_id, raw_name),
                )

        if extra_review_note:
            review_notes = (
                f"{review_notes}; {extra_review_note}"
                if review_notes
                else extra_review_note
            )

        cur = conn.execute(
            """
            INSERT INTO assessments (
                person_id, source_file_sha256, source_page_index, source_page_key,
                page_image_path, test_version, test_form, extraction_skill, klasse,
                gender, test_date_raw, test_date_iso, norm_variant, assessment_note,
                illegible_notes, model_confidence, status, flags, review_notes,
                raw_response_json, model_id, runner
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                final_person_id,
                file_sha256,
                page_index,
                page_key,
                page_image_path,
                extracted.test_version,
                extracted.test_form,
                extraction_skill,
                extracted.klasse.upper() if extracted.klasse else None,
                extracted.gender,
                extracted.test_date_raw,
                extracted.test_date_iso,
                norm_variant,
                extracted.assessment_note,
                extracted.illegible_notes,
                extracted.overall_confidence,
                "needs_review",
                json.dumps(flags),
                review_notes,
                json.dumps(raw_response, ensure_ascii=False),
                model_id,
                runner,
            ),
        )
        assessment_id = cur.lastrowid

        metric_by_key = (
            {m.key: m for m in definition.overall_metrics} if definition else {}
        )
        for position, mv in enumerate(extracted.overall, start=1):
            defn_metric = metric_by_key.get(mv.key)
            value_num: float | None = None
            value_text: str | None = None
            if defn_metric is not None and defn_metric.value_type in ("int", "real"):
                value_num = coerce_float(mv.value)
            elif defn_metric is not None and defn_metric.value_type == "text":
                value_text = mv.value
            else:
                # No definition, or an unknown metric key: keep the raw value
                # visible in value_text too, best-effort, alongside value_raw.
                value_text = mv.value
            # ADR 0013: 'blank' only when the model reported an empty cell
            # for a metric the definition actually declares — a value
            # always wins over a state, and completion never assigns
            # 'unavailable' here (only the planner does).
            value_provenance = (
                "blank"
                if mv.value is None
                and mv.value_state == "blank"
                and defn_metric is not None
                else "sheet"
            )
            conn.execute(
                "INSERT INTO assessment_metrics "
                "(assessment_id, position, metric_key, value_raw, value_num, value_text, "
                "confidence, value_provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assessment_id,
                    position,
                    mv.key,
                    mv.value,
                    value_num,
                    value_text,
                    mv.confidence,
                    value_provenance,
                ),
            )

        sum_targets = definition.subtest_sum_targets() if definition else set()
        for position, st in enumerate(extracted.subtests, start=1):
            canonical_key = st.key
            printed_label = st.key
            if definition is not None:
                resolved = test_registry.canonical_subtest_key(definition, st.key)
                if resolved is not None:
                    canonical_key = resolved

            defn_subtest = None
            if definition is not None:
                defn_subtest = next(
                    (s for s in definition.subtests if s.key == canonical_key), None
                )
            value_type = defn_subtest.value_type if defn_subtest else "real"
            rw_num = coerce_float(st.rw)
            rw_max_num = coerce_float(st.rw_max)
            if rw_max_num is None and defn_subtest is not None:
                rw_max_num = defn_subtest.rw_max
            if value_type == "int" and rw_num is not None:
                rw_num = float(int(rw_num)) if rw_num == int(rw_num) else rw_num
            # ADR 0013: a value always wins over a state; completion never
            # assigns 'unavailable' here (only the planner does).
            pr_provenance = (
                "blank" if st.pr is None and st.pr_state == "blank" else "sheet"
            )
            # ADR 0015: only a sum-target subtest's RW (DEMAT 3+ ARI/SAR/GEO)
            # can be left for the app — 'blank' iff empty and reported so.
            # Illegible / None stay 'sheet' and are never filled.
            rw_provenance = (
                "blank"
                if canonical_key in sum_targets
                and rw_num is None
                and st.rw_state == "blank"
                else "sheet"
            )

            conn.execute(
                "INSERT INTO subtest_scores "
                "(assessment_id, position, subtest_key, printed_label, name, value_raw, "
                "rw, rw_max, pr, confidence, pr_provenance, rw_provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assessment_id,
                    position,
                    canonical_key,
                    printed_label,
                    st.name,
                    st.rw,
                    rw_num,
                    rw_max_num,
                    st.pr,
                    st.confidence,
                    pr_provenance,
                    rw_provenance,
                ),
            )

    return {
        "assessment_id": assessment_id,
        "person_id": final_person_id,
        "flags": flags,
        "has_blocking_flags": has_blocking_flags,
    }


_AGNOSTIC_COLUMNS = [
    "assessment_id",
    "person_id",
    "klasse",
    "gender",
    "test_version",
    "test_form",
    "test_date_iso",
    "norm_variant",
    "assessment_note",
    "status",
    "flags",
    "review_notes",
    "page_image_path",
    "extraction_skill",
    "model_confidence",
    "created_at",
    "opened_at",
    "edited_at",
    "finalized_at",
    "source_file_sha256",
    "source_page_index",
]


def list_assessments(filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return assessments as a list of dict rows, agnostic columns plus a
    pivot of `assessment_metrics` for the filtered `test_version` (or the
    union of metric keys across returned rows, when unfiltered). Excludes
    raw names by design.
    """
    filters = filters or {}
    clauses = []
    params: list[Any] = []

    if filters.get("test_version"):
        clauses.append("test_version = ?")
        params.append(filters["test_version"])
    if filters.get("klasse"):
        clauses.append("klasse = ?")
        params.append(filters["klasse"])
    if filters.get("status"):
        clauses.append("status = ?")
        params.append(filters["status"])
    if filters.get("person_id"):
        clauses.append("person_id LIKE ?")
        params.append(f"%{filters['person_id']}%")
    if filters.get("date_from"):
        clauses.append("test_date_iso >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("test_date_iso <= ?")
        params.append(filters["date_to"])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {", ".join(_AGNOSTIC_COLUMNS)}
        FROM assessments
        {where}
        ORDER BY created_at DESC
    """
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not rows:
            return rows

        assessment_ids = [r["assessment_id"] for r in rows]
        placeholders = ",".join("?" * len(assessment_ids))
        metric_rows = conn.execute(
            f"SELECT assessment_id, metric_key, value_raw, value_num, value_text "
            f"FROM assessment_metrics WHERE assessment_id IN ({placeholders})",
            assessment_ids,
        ).fetchall()

    metrics_by_assessment: dict[int, dict[str, Any]] = {}
    for mr in metric_rows:
        value = (
            mr["value_num"]
            if mr["value_num"] is not None
            else (mr["value_text"] if mr["value_text"] is not None else mr["value_raw"])
        )
        metrics_by_assessment.setdefault(mr["assessment_id"], {})[mr["metric_key"]] = (
            value
        )

    for r in rows:
        r.update(metrics_by_assessment.get(r["assessment_id"], {}))

    return rows


def get_subtest_scores(assessment_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT position, subtest_key, printed_label, name, value_raw, rw, rw_max, "
            "pr, confidence, pr_provenance, rw_provenance "
            "FROM subtest_scores WHERE assessment_id = ? ORDER BY position",
            (assessment_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_assessment_metrics(assessment_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT position, metric_key, value_raw, value_num, value_text, confidence, "
            "value_provenance "
            "FROM assessment_metrics WHERE assessment_id = ? ORDER BY position",
            (assessment_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_person_history(person_id: str) -> list[dict[str, Any]]:
    """All assessments for one person, oldest first, for cross-test history."""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_AGNOSTIC_COLUMNS)} FROM assessments "
            "WHERE person_id = ? ORDER BY test_date_iso ASC, created_at ASC",
            (person_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_assessment(assessment_id: int) -> dict[str, Any] | None:
    """One assessment's full agnostic-column row plus `source_files
    .stored_pdf_path`, for the Inbox review panel (edit form + "open source
    document"). Not in the spec's enumerated function list, but required by
    it: the review panel needs `gender`/`test_form`/`test_date_raw`
    /`assessment_note`/`review_notes`/`status`, none of which
    `list_inbox_assessments()` returns, plus the stored PDF path.

    Also joins the most recently seen raw name from `person_names` as
    `person_name`, read-only display only (never editable — `person_id`
    stays the identity key, per ADR 0001/0006). NULL for an unlinked row
    or when `config.STORE_RAW_NAMES` is off. `list_assessments` (Results)
    deliberately does not do this join; raw names stay scoped to the
    single-assessment review panel.
    """
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_AGNOSTIC_COLUMNS)}, "
            "test_date_raw, review_notes, review_validation_json, "
            "review_validation_at, review_validation_edited_at, "
            "(SELECT stored_pdf_path FROM source_files "
            " WHERE source_files.file_sha256 = assessments.source_file_sha256) "
            " AS stored_pdf_path, "
            "(SELECT raw_name FROM person_names "
            " WHERE person_names.person_id = assessments.person_id "
            " ORDER BY first_seen DESC LIMIT 1) AS person_name "
            "FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["flags"] = json.loads(d["flags"]) if d["flags"] else []
    return d


def get_reextract_target(assessment_id: int) -> dict[str, Any] | None:
    """What re-extract (ADR 0016) needs to know about the row it replaces:
    its source page identity, the stored PDF path, and the only state that
    carries over (`person_id`, `norm_variant`) or is audited (`status`,
    `edited_at`, `finalized_at`). None if the row does not exist. A row with
    a NULL `source_file_sha256` is returned as is (the caller skips it).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT assessment_id, source_file_sha256, source_page_index, "
            "source_page_key, page_image_path, person_id, norm_variant, status, "
            "edited_at, finalized_at, "
            "(SELECT stored_pdf_path FROM source_files "
            " WHERE source_files.file_sha256 = assessments.source_file_sha256) "
            " AS stored_pdf_path "
            "FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_inbox_assessments(
    statuses: list[str] | None = None, order: str = "desc"
) -> list[dict[str, Any]]:
    """Assessments matching `statuses` (default `['needs_review']`), single
    list, sorted by `created_at` per `order` (ADR 0007). Read/unread is
    `opened_at`-driven UI styling, not a partition of this query.
    """
    statuses = statuses or ["needs_review"]
    direction = "ASC" if order == "asc" else "DESC"
    placeholders = ",".join("?" * len(statuses))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT assessment_id, person_id, klasse, test_version, test_date_iso,
                   status, created_at, opened_at, edited_at, flags,
                   source_file_sha256, page_image_path,
                   review_validation_json, review_validation_at
            FROM assessments
            WHERE status IN ({placeholders})
            ORDER BY created_at {direction}
            """,
            statuses,
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d["flags"]) if d["flags"] else []
        result.append(d)
    return result


def mark_opened(assessment_id: int) -> None:
    """Stamp `opened_at` if NULL; no-op otherwise (idempotent)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE assessments SET opened_at = datetime('now') "
            "WHERE assessment_id = ? AND opened_at IS NULL",
            (assessment_id,),
        )


def _mark_edited(conn: sqlite3.Connection, assessment_id: int, summary: str) -> None:
    """Stamp `edited_at` if NULL, append a manual-edit detail to
    `review_notes` (ADR 0007). Never touches `status` or
    `raw_response_json`. Called by every updater, in the caller's
    transaction.
    """
    note = f"manual edit: {summary}"
    row = conn.execute(
        "SELECT review_notes FROM assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    existing_notes = row["review_notes"] if row else None
    new_notes = f"{existing_notes}; {note}" if existing_notes else note
    conn.execute(
        "UPDATE assessments SET "
        "edited_at = COALESCE(edited_at, datetime('now')), "
        "review_notes = ? "
        "WHERE assessment_id = ?",
        (new_notes, assessment_id),
    )


def update_assessment_fields(assessment_id: int, changes: dict[str, Any]) -> None:
    """Update whitelisted `assessments` scalar columns (ADR 0007). Raises on
    any key outside `_EDITABLE_ASSESSMENT_FIELDS`. `person_id` is
    deliberately not editable here.

    `norm_variant` (ADR 0010): `""` normalises to `None`; a non-None value is
    validated against the row's resolved `norm_test` (read via the row's
    stored `test_version`, same pattern `update_assessment_metric` uses). If
    the row's `test_version` is NULL or unrecognised, any value present in
    *any* test's variant list is accepted — an unrecognised-version row must
    stay editable (ADR 0005 §3). Raises `ValueError` on an invalid value.
    """
    if not changes:
        return
    unknown = set(changes) - _EDITABLE_ASSESSMENT_FIELDS
    if unknown:
        raise ValueError(f"not editable: {sorted(unknown)}")
    if changes.get("klasse"):
        changes = {**changes, "klasse": changes["klasse"].upper()}
    if "norm_variant" in changes:
        value = changes["norm_variant"] or None
        changes = {**changes, "norm_variant": value}

    columns = list(changes.keys())
    set_clause = ", ".join(f"{c} = ?" for c in columns)
    summary = ", ".join(f"{c}={changes[c]!r}" for c in columns)

    with get_connection() as conn:
        if "norm_variant" in changes and changes["norm_variant"] is not None:
            value = changes["norm_variant"]
            test_version = conn.execute(
                "SELECT test_version FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            test_version = test_version["test_version"] if test_version else None
            defn = test_registry.get(conn, test_version) if test_version else None
            if defn is not None and defn.norm_test is not None:
                if not norm_variants.is_valid(defn.norm_test, value):
                    raise ValueError(
                        f"norm_variant {value!r} is not valid for norm_test "
                        f"{defn.norm_test!r} (valid: "
                        f"{[v.key for v in norm_variants.variants_for(defn.norm_test)]})"
                    )
            else:
                any_valid = any(
                    norm_variants.is_valid(nt, value) for nt in norm_variants.VARIANTS
                )
                if not any_valid:
                    raise ValueError(
                        f"norm_variant {value!r} is not a recognised variant for "
                        "any test"
                    )

        conn.execute(
            f"UPDATE assessments SET {set_clause} WHERE assessment_id = ?",
            (*[changes[c] for c in columns], assessment_id),
        )
        _mark_edited(conn, assessment_id, summary)


def rename_assessment_person(assessment_id: int, new_raw_name: str) -> dict[str, Any]:
    """Correct the child's name for one assessment.

    `person_id`, once allocated to an assessment, is permanent — a later
    name correction never changes it, and never re-runs collision/merge
    checks. Only the stored spelling changes (a new `person_names` row
    for the existing `person_id`).

    An assessment that has never been allocated a `person_id` (unlinked:
    NULL) treats this as a *first* allocation, so it goes through the
    same collision-checked resolution `save_assessment` uses — that
    first allocation is exactly where collisions must be caught.

    Always stamps `edited_at`/`review_notes` (ADR 0007) — a corrected
    name is exactly the kind of change `validated_with_changes` exists
    to record. Raises if `config.STORE_RAW_NAMES` is False (nowhere to
    persist a raw name) or `new_raw_name` is blank.
    """
    if not config.STORE_RAW_NAMES:
        raise ValueError("name editing is disabled (DEMAT_STORE_RAW_NAMES=false)")
    new_raw_name = (new_raw_name or "").strip()
    if not new_raw_name:
        raise ValueError("name cannot be blank")

    with get_connection() as conn:
        current = conn.execute(
            "SELECT person_id, test_version, test_date_iso, gender "
            "FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"no assessment {assessment_id}")

        if current["person_id"] is not None:
            # Already allocated: person_id is permanent. Record the
            # corrected spelling against the existing identity only.
            final_person_id = current["person_id"]
            note = f"name corrected to {new_raw_name!r}"
            if _find_person_by_id(conn, final_person_id) is None:
                # Data predates the FK guarantee (or was inserted outside
                # the app): self-heal by backfilling `persons` under the
                # existing, unchanged person_id rather than crashing.
                # normalization_key is best-effort — the original name
                # that produced this person_id isn't recoverable, so the
                # corrected name is the closest available signal.
                upsert_person(
                    conn, final_person_id, identity.normalization_key(new_raw_name)
                )
                note += " (backfilled missing persons row for this person_id)"
            conn.execute(
                "INSERT OR IGNORE INTO person_names (person_id, raw_name) "
                "VALUES (?, ?)",
                (final_person_id, new_raw_name),
            )
        else:
            # First allocation for this assessment: same collision-checked
            # resolution as save_assessment's initial linkage — including
            # the gender-mismatch and duplicate-date checks (ADR 0006 §3/§4),
            # which this path used to skip entirely. That gap is how a real
            # incident happened: two same-named, different-gender children
            # merged silently because a name typed in here never got the
            # gender check `save_assessment` already had.
            salt = config.resolve_id_salt()
            new_person_id, new_key = identity.person_id_for(new_raw_name, salt)
            was_new = _find_person_by_id(conn, new_person_id) is None
            final_person_id, extra_flags, link_note = _resolve_person_link(
                conn,
                computed_person_id=new_person_id,
                normalization_key=new_key,
                raw_name=new_raw_name,
                incoming_gender=current["gender"],
                test_version=current["test_version"],
                test_date_iso=current["test_date_iso"],
            )
            if final_person_id is None or extra_flags:
                note = f"name set to {new_raw_name!r}; {link_note}"
            elif was_new:
                note = f"name set to {new_raw_name!r}; allocated person_id"
            else:
                note = f"name set to {new_raw_name!r}; linked to existing person_id"

            if final_person_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO person_names (person_id, raw_name) "
                    "VALUES (?, ?)",
                    (final_person_id, new_raw_name),
                )
            conn.execute(
                "UPDATE assessments SET person_id = ? WHERE assessment_id = ?",
                (final_person_id, assessment_id),
            )
            if extra_flags:
                flags_row = conn.execute(
                    "SELECT flags FROM assessments WHERE assessment_id = ?",
                    (assessment_id,),
                ).fetchone()
                current_flags = (
                    json.loads(flags_row["flags"]) if flags_row["flags"] else []
                )
                for flag in extra_flags:
                    if flag not in current_flags:
                        current_flags.append(flag)
                conn.execute(
                    "UPDATE assessments SET flags = ? WHERE assessment_id = ?",
                    (json.dumps(current_flags), assessment_id),
                )

        _mark_edited(conn, assessment_id, note)

    return {"person_id": final_person_id}


def _norm_str_for_compare(x: Any) -> str | None:
    """None and '' compare equal (both "nothing entered") — everything
    else compares by its stripped string form.
    """
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def update_assessment_metric(
    assessment_id: int, position: int, value_raw: str | None
) -> None:
    """Rewrite one `assessment_metrics` row's `value_raw`, recomputing
    `value_num`/`value_text` from the metric's `value_type` (via
    `test_definition_metrics`), mirroring `save_assessment`'s coercion.

    `value_provenance` (ADR 0013): a reviewer edit that actually changes the
    value marks the cell `manual`, so a later completion run never touches
    it again. An edit that leaves the value unchanged (e.g. re-saving the
    same RW) does not disturb an existing `computed`/`blank`/`unavailable`
    provenance — the next completion run still owns and re-evaluates it.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT metric_key, value_raw AS old_value_raw "
            "FROM assessment_metrics WHERE assessment_id = ? AND position = ?",
            (assessment_id, position),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"no metric at position {position} for assessment {assessment_id}"
            )
        metric_key = row["metric_key"]
        changed = _norm_str_for_compare(row["old_value_raw"]) != _norm_str_for_compare(
            value_raw
        )

        test_version = conn.execute(
            "SELECT test_version FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()["test_version"]

        value_type = None
        if test_version is not None:
            defn_row = conn.execute(
                "SELECT value_type FROM test_definition_metrics "
                "WHERE test_version = ? AND metric_key = ?",
                (test_version, metric_key),
            ).fetchone()
            if defn_row is not None:
                value_type = defn_row["value_type"]

        value_num: float | None = None
        value_text: str | None = None
        if value_type in ("int", "real"):
            value_num = coerce_float(value_raw)
        elif value_type == "text":
            value_text = value_raw
        else:
            value_text = value_raw

        if changed:
            conn.execute(
                "UPDATE assessment_metrics SET value_raw = ?, value_num = ?, "
                "value_text = ?, value_provenance = 'manual' "
                "WHERE assessment_id = ? AND position = ?",
                (value_raw, value_num, value_text, assessment_id, position),
            )
        else:
            conn.execute(
                "UPDATE assessment_metrics SET value_raw = ?, value_num = ?, "
                "value_text = ? "
                "WHERE assessment_id = ? AND position = ?",
                (value_raw, value_num, value_text, assessment_id, position),
            )
        _mark_edited(
            conn, assessment_id, f"metric[{position}] {metric_key}={value_raw!r}"
        )


def _norm_pr_for_compare(x: Any) -> int | None:
    """`st.data_editor` round-trips INTEGER `pr` as float and a blank cell
    as NaN — normalize both `None`/NaN to `None`, everything else to a
    rounded `int`, so a same-valued edit doesn't look like a change.
    """
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f)


def _norm_rw_for_compare(x: Any) -> float | None:
    """Same idea as `_norm_pr_for_compare` for REAL `rw` (half points
    allowed): `None`/NaN -> `None`, otherwise `round(float(x), 6)`."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, 6)


def update_subtest_score(
    assessment_id: int,
    position: int,
    *,
    value_raw: str | None = None,
    rw: float | None = None,
    rw_max: float | None = None,
    pr: int | None = None,
) -> None:
    """Rewrite one `subtest_scores` row's scores, same coercion rules as
    `save_assessment` (rw/rw_max are stored numeric already; the caller
    supplies coerced values for rw/rw_max, matching what the edit form's
    `st.data_editor` round-trips).

    `pr_provenance` (ADR 0013): only an actual change to `pr` marks the
    cell `manual`. An RW-only edit must not freeze a looked-up PR — the
    next completion run re-evaluates it against the new RW.

    `rw_provenance` (ADR 0015): independently, an actual change to `rw`
    marks the RW cell `manual`. Editing a component (e.g. ZS) does not touch
    its sum target — the next completion run re-derives it if still owned.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT subtest_key, pr AS old_pr, rw AS old_rw FROM subtest_scores "
            "WHERE assessment_id = ? AND position = ?",
            (assessment_id, position),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"no subtest at position {position} for assessment {assessment_id}"
            )
        subtest_key = row["subtest_key"]
        pr_changed = _norm_pr_for_compare(row["old_pr"]) != _norm_pr_for_compare(pr)
        rw_changed = _norm_rw_for_compare(row["old_rw"]) != _norm_rw_for_compare(rw)

        extra_sets = ""
        if pr_changed:
            extra_sets += ", pr_provenance = 'manual'"
        if rw_changed:
            extra_sets += ", rw_provenance = 'manual'"
        conn.execute(
            "UPDATE subtest_scores SET value_raw = ?, rw = ?, rw_max = ?, pr = ?"
            f"{extra_sets} WHERE assessment_id = ? AND position = ?",
            (value_raw, rw, rw_max, pr, assessment_id, position),
        )
        _mark_edited(
            conn,
            assessment_id,
            f"subtest[{position}] {subtest_key} value_raw={value_raw!r} "
            f"rw={rw!r} rw_max={rw_max!r} pr={pr!r}",
        )


def _assessment_has_computed_cell(conn: sqlite3.Connection, assessment_id: int) -> bool:
    """Any `computed` cell — a norm lookup or an RW sum (ADR 0013/0015)."""
    row = conn.execute(
        "SELECT 1 FROM subtest_scores WHERE assessment_id = ? AND "
        "(pr_provenance = 'computed' OR rw_provenance = 'computed') "
        "UNION SELECT 1 FROM assessment_metrics WHERE assessment_id = ? "
        "AND value_provenance = 'computed' LIMIT 1",
        (assessment_id, assessment_id),
    ).fetchone()
    return row is not None


def has_computed_lookup(
    subtest_rows: list[dict[str, Any]],
    metrics_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> bool:
    """True iff a *norm lookup* value is `computed` — a subtest PR, or a
    metric that is not itself a sum target (ADR 0015). A row whose only
    computed cells are RW sums returns False. Derived from the provenance
    columns `get_subtest_scores`/`get_assessment_metrics` already return.
    """
    if any(r.get("pr_provenance") == "computed" for r in subtest_rows):
        return True
    sum_metrics = (
        {r.metric for r in definition.sum_rules_metric()} if definition else set()
    )
    sum_metrics.add("rw_total")
    return any(
        r.get("value_provenance") == "computed" and r["metric_key"] not in sum_metrics
        for r in metrics_rows
    )


def _current_flags(conn: sqlite3.Connection, assessment_id: int) -> list[str]:
    row = conn.execute(
        "SELECT flags FROM assessments WHERE assessment_id = ?", (assessment_id,)
    ).fetchone()
    if row is None or not row["flags"]:
        return []
    return list(json.loads(row["flags"]))


def apply_norm_completion(assessment_id: int, plan: CompletionPlan) -> dict[str, Any]:
    """Persist one `norm_completion.CompletionPlan` (ADR 0013), as one
    transaction:

    - Status guard: a non-`needs_review` row is left untouched.
    - Per-write guard: re-reads each cell's current provenance and only
      writes if it is still in `COMPLETION_OWNED` (protects against a
      concurrent reviewer edit).
    - Never calls `_mark_edited` / touches `edited_at`, `status` or
      `raw_response_json` — a system lookup is not a human edit (I6).
    - `norm_values_computed` is present exactly when any `computed` cell
      exists anywhere on the row, added/removed accordingly; other flags
      and their order are left unchanged.
    - Appends one neutral `review_notes` line, only when a write happened;
      never rewrites earlier text.

    Returns {"computed": n, "unavailable": u, "blank": b, "flags": [...]}.
    """
    result: dict[str, Any] = {"computed": 0, "unavailable": 0, "blank": 0, "flags": []}

    if plan.kb_unavailable is not None:
        with get_connection() as conn:
            result["flags"] = _current_flags(conn, assessment_id)
        return result

    with get_connection() as conn:
        status_row = conn.execute(
            "SELECT status FROM assessments WHERE assessment_id = ?", (assessment_id,)
        ).fetchone()
        if status_row is None or status_row["status"] != "needs_review":
            result["flags"] = _current_flags(conn, assessment_id)
            return result

        applied = []
        for write in plan.writes:
            if write.table == "subtest" and write.field == "rw":
                # ADR 0015: an RW sum on a sum-target subtest.
                current = conn.execute(
                    "SELECT rw_provenance FROM subtest_scores "
                    "WHERE assessment_id = ? AND position = ?",
                    (assessment_id, write.position),
                ).fetchone()
                if current is None:
                    continue
                current_provenance = current["rw_provenance"] or "sheet"
                if current_provenance not in COMPLETION_OWNED:
                    continue
                conn.execute(
                    "UPDATE subtest_scores SET rw = ?, value_raw = ?, rw_provenance = ? "
                    "WHERE assessment_id = ? AND position = ?",
                    (
                        write.rw,
                        write.value_raw,
                        write.provenance,
                        assessment_id,
                        write.position,
                    ),
                )
            elif write.table == "subtest":
                current = conn.execute(
                    "SELECT pr_provenance FROM subtest_scores "
                    "WHERE assessment_id = ? AND position = ?",
                    (assessment_id, write.position),
                ).fetchone()
                if current is None:
                    continue
                current_provenance = current["pr_provenance"] or "sheet"
                if current_provenance not in COMPLETION_OWNED:
                    continue
                conn.execute(
                    "UPDATE subtest_scores SET pr = ?, pr_provenance = ? "
                    "WHERE assessment_id = ? AND position = ?",
                    (write.pr, write.provenance, assessment_id, write.position),
                )
            else:
                current = conn.execute(
                    "SELECT value_provenance FROM assessment_metrics "
                    "WHERE assessment_id = ? AND position = ?",
                    (assessment_id, write.position),
                ).fetchone()
                if current is None:
                    continue
                current_provenance = current["value_provenance"] or "sheet"
                if current_provenance not in COMPLETION_OWNED:
                    continue
                conn.execute(
                    "UPDATE assessment_metrics SET value_raw = ?, value_num = ?, "
                    "value_text = ?, value_provenance = ? "
                    "WHERE assessment_id = ? AND position = ?",
                    (
                        write.value_raw,
                        write.value_num,
                        write.value_text,
                        write.provenance,
                        assessment_id,
                        write.position,
                    ),
                )
            applied.append(write)
            if write.provenance == "computed":
                result["computed"] += 1
            elif write.provenance == "unavailable":
                result["unavailable"] += 1
            elif write.provenance == "blank":
                result["blank"] += 1

        flags = _current_flags(conn, assessment_id)
        has_computed = _assessment_has_computed_cell(conn, assessment_id)
        if has_computed and "norm_values_computed" not in flags:
            flags.append("norm_values_computed")
        elif not has_computed and "norm_values_computed" in flags:
            flags.remove("norm_values_computed")
        conn.execute(
            "UPDATE assessments SET flags = ? WHERE assessment_id = ?",
            (json.dumps(flags), assessment_id),
        )
        result["flags"] = flags

        # A PR cell on a subtest with no norm scale (DEMAT 3+ Aufgabentyp rows)
        # becoming 'unavailable' is not news — keep it out of the counts, or
        # every 3+ sheet would report "10 im Manual nicht veröffentlicht".
        no_scale_refs = {
            s.field_ref for s in plan.skipped if s.reason == "no_norm_scale"
        }
        counted = [
            w
            for w in applied
            if not (
                w.provenance == "unavailable"
                and w.field == "pr"
                and w.field_ref in no_scale_refs
            )
        ]
        result["unavailable"] = sum(1 for w in counted if w.provenance == "unavailable")

        if counted:
            group_label = plan.norm_group or "keine Normtabelle"
            n = sum(1 for w in counted if w.provenance == "computed")
            u = result["unavailable"]
            b = sum(1 for w in counted if w.provenance == "blank")
            note = (
                f"Normwerte nachgeschlagen ({group_label}): {n} berechnet, "
                f"{u} im Manual nicht veröffentlicht, {b} offen"
            )
            if b:
                # An RW sum's only blank reason is `sum_incomplete`; a PR on
                # the same subtest has its own reason (e.g. `rw_missing`).
                reason_by_ref = {
                    s.field_ref: s.reason
                    for s in plan.skipped
                    if not (
                        s.reason == "sum_incomplete"
                        and s.field_ref.startswith("subtest:")
                    )
                }
                detail_pairs = []
                for w in counted:
                    if w.provenance != "blank":
                        continue
                    if w.field == "rw":
                        detail_pairs.append(f"{w.field_ref}.rw=sum_incomplete")
                    elif w.field_ref in reason_by_ref:
                        detail_pairs.append(
                            f"{w.field_ref}={reason_by_ref[w.field_ref]}"
                        )
                if detail_pairs:
                    note += ": " + ", ".join(detail_pairs)
            existing_row = conn.execute(
                "SELECT review_notes FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            existing_notes = existing_row["review_notes"] if existing_row else None
            new_notes = f"{existing_notes}; {note}" if existing_notes else note
            conn.execute(
                "UPDATE assessments SET review_notes = ? WHERE assessment_id = ?",
                (new_notes, assessment_id),
            )

    return result


def list_known_klassen() -> list[str]:
    """Sorted distinct non-NULL `klasse` values across all assessments, for
    autocomplete in the Inbox edit form.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT klasse FROM assessments WHERE klasse IS NOT NULL "
            "ORDER BY klasse"
        ).fetchall()
    return [r["klasse"] for r in rows]


def list_class_groups() -> list[dict[str, Any]]:
    """Group `assessments` by the class key `(upper(klasse),
    school_year(test_date_iso), test_version)` (ADR 0008 spec §4.2).

    Cannot group by Schuljahr in SQL — it's derived from `test_date_iso` in
    Python, not stored — so this selects candidate rows for both status sets
    (validated + `needs_review`) and groups them here via
    `school_year.school_year()`. Safe to call that helper directly (its
    today-fallback never triggers) because every candidate row already has
    a non-NULL `test_date_iso`; a row whose date fails to parse is dropped
    from grouping entirely (§4.2's ungrouped rule), same as a NULL
    klasse/test_version/date.

    Returns one dict per key: `klasse`, `schuljahr`, `test_version`,
    `assessment_ids` (validated ids only — the staleness comparison set,
    §4.4), `child_count`, `awaiting_review_count` (needs_review rows over
    the same key), `test_forms`, `test_date_min`, `test_date_max` (over the
    validated rows), `norm_variants` (sorted distinct non-NULL `norm_variant`
    values over the validated rows, ADR 0010) and `norm_variant` (that single
    value when unanimous, else `None` — NULLs abstain, so one unrecorded
    sheet among many asserted ones does not discard the assertion; distinct
    from "mixed", which also yields `None` but leaves >1 entries in
    `norm_variants`). A key with `child_count == 0` (only needs_review rows
    contributed) is still returned; callers apply §4.3's "listed" rule.
    """
    from demat.school_year import school_year as _school_year

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT assessment_id, klasse, test_version, test_form, test_date_iso, "
            "norm_variant, status "
            "FROM assessments "
            "WHERE klasse IS NOT NULL AND test_version IS NOT NULL "
            "AND test_date_iso IS NOT NULL "
            "AND status IN ('validated_ok', 'validated_with_changes', 'needs_review')"
        ).fetchall()

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        sy = _school_year(r["test_date_iso"])
        if sy is None:
            continue
        key = (r["klasse"].upper(), sy, r["test_version"])
        g = groups.setdefault(
            key,
            {
                "klasse": key[0],
                "schuljahr": key[1],
                "test_version": key[2],
                "assessment_ids": [],
                "awaiting_review_count": 0,
                "test_forms": set(),
                "test_date_min": None,
                "test_date_max": None,
                "norm_variants": set(),
            },
        )
        if r["status"] == "needs_review":
            g["awaiting_review_count"] += 1
            continue
        g["assessment_ids"].append(r["assessment_id"])
        if r["test_form"]:
            g["test_forms"].add(r["test_form"])
        if r["norm_variant"]:
            g["norm_variants"].add(r["norm_variant"])
        d = r["test_date_iso"]
        if g["test_date_min"] is None or d < g["test_date_min"]:
            g["test_date_min"] = d
        if g["test_date_max"] is None or d > g["test_date_max"]:
            g["test_date_max"] = d

    result = []
    for g in groups.values():
        g["child_count"] = len(g["assessment_ids"])
        g["test_forms"] = sorted(g["test_forms"])
        norm_variant_list = sorted(g["norm_variants"])
        g["norm_variants"] = norm_variant_list
        g["norm_variant"] = (
            norm_variant_list[0] if len(norm_variant_list) == 1 else None
        )
        result.append(g)
    return result


def list_test_definitions() -> list[TestDefinition]:
    """All active test definitions (ADR 0010), for the upload/review
    Normtabelle selectors. Thin wrapper over `test_registry.all_active`,
    matching `get_test_definition`'s connection style.
    """
    with get_connection() as conn:
        return test_registry.all_active(conn)


def count_ungrouped_validated_assessments() -> int:
    """Count of `validated_ok`/`validated_with_changes` assessments that
    belong to no class — NULL `klasse`/`test_version`/`test_date_iso`, or a
    `test_date_iso` that fails to parse (ADR 0008 spec §4.2). Surfaced on
    the Class Profile page as an `st.caption`, never silently dropped.
    """
    from demat.school_year import school_year as _school_year

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT klasse, test_version, test_date_iso FROM assessments "
            "WHERE status IN ('validated_ok', 'validated_with_changes')"
        ).fetchall()

    count = 0
    for r in rows:
        if not r["klasse"] or not r["test_version"] or not r["test_date_iso"]:
            count += 1
        elif _school_year(r["test_date_iso"]) is None:
            count += 1
    return count


def get_class_assessments(
    klasse: str, schuljahr: str, test_version: str
) -> list[dict[str, Any]]:
    """Validated assessments (`validated_ok`/`validated_with_changes`) for
    one class key. `schuljahr` is derived, not stored, so this filters in
    Python after the SQL narrows by the stored columns.
    """
    from demat.school_year import school_year as _school_year

    klasse_upper = klasse.upper()
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_AGNOSTIC_COLUMNS)} FROM assessments "
            "WHERE klasse = ? AND test_version = ? "
            "AND status IN ('validated_ok', 'validated_with_changes') "
            "AND test_date_iso IS NOT NULL "
            "ORDER BY test_date_iso ASC, created_at ASC",
            (klasse_upper, test_version),
        ).fetchall()
    return [dict(r) for r in rows if _school_year(r["test_date_iso"]) == schuljahr]


def latest_class_profile(
    klasse: str, schuljahr: str, test_version: str
) -> dict[str, Any] | None:
    """Most recent `class_profiles` row for one class key, or None if the
    class has never been generated (ADR 0008 spec §4.4's "new" state).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM class_profiles "
            "WHERE klasse = ? AND schuljahr = ? AND test_version = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (klasse.upper(), schuljahr, test_version),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["assessment_ids"] = json.loads(d["assessment_ids_json"])
    return d


def save_class_profile(
    *,
    klasse: str,
    schuljahr: str,
    test_version: str,
    test_form_summary: str | None,
    test_date_min: str | None,
    test_date_max: str | None,
    norm_group: str | None,
    classification_basis: str | None,
    child_count: int,
    assessment_ids: list[int],
    markdown_path: str,
    content_hash: str | None = None,
    pdf_path: str | None = None,
    prose_run_dir: str | None = None,
    model_id: str | None = None,
    runner: str | None = None,
) -> int:
    """Insert a new `class_profiles` row (append-only — ADR 0008 spec §7).
    Regenerating a profile always inserts; it never updates an existing row.
    Returns the new `profile_id`.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO class_profiles (
                klasse, schuljahr, test_version, test_form_summary,
                test_date_min, test_date_max, norm_group, classification_basis,
                child_count, assessment_ids_json, content_hash, markdown_path,
                pdf_path, prose_run_dir, model_id, runner
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                klasse.upper(),
                schuljahr,
                test_version,
                test_form_summary,
                test_date_min,
                test_date_max,
                norm_group,
                classification_basis,
                child_count,
                json.dumps(assessment_ids),
                content_hash,
                markdown_path,
                pdf_path,
                prose_run_dir,
                model_id,
                runner,
            ),
        )
        profile_id = cur.lastrowid
    assert profile_id is not None
    return profile_id


def latest_child_report(assessment_id: int) -> dict[str, Any] | None:
    """Most recent `child_reports` row for one assessment, or `None` if it
    has never been generated (ADR 0014 spec §9, same "new" state as
    `latest_class_profile`).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM child_reports WHERE assessment_id = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_child_reports(person_ref: str) -> list[dict[str, Any]]:
    """Every `child_reports` row for one `person_ref`, newest first —
    ADR 0014 spec §9's per-person history (paths keyed by `person_ref`,
    never by name).
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM child_reports WHERE person_ref = ? "
            "ORDER BY generated_at DESC",
            (person_ref,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_child_report(
    *,
    assessment_id: int,
    person_ref: str,
    test_version: str,
    markdown_path: str,
    template_version: str,
    content_hash: str | None = None,
) -> int:
    """Insert a new `child_reports` row (append-only — ADR 0014 spec §9,
    same precedent as `save_class_profile`). Regenerating always inserts;
    it never updates an existing row. Returns the new `report_id`.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO child_reports (
                assessment_id, person_ref, test_version, content_hash,
                markdown_path, template_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                assessment_id,
                person_ref,
                test_version,
                content_hash,
                markdown_path,
                template_version,
            ),
        )
        report_id = cur.lastrowid
    assert report_id is not None
    return report_id


def save_review_validation(
    assessment_id: int, findings_json: str, edited_at_snapshot: str | None
) -> None:
    """Persist the result of `review_validation.run_review_validations()`
    (ADR-pending review-time validation section): `findings_json` is
    `review_validation.findings_to_json(...)`. `edited_at_snapshot` is the
    assessment's `edited_at` *at the time this ran* — read back and compared
    against the current `edited_at` to detect a result gone stale after a
    later manual edit. Never touches `status`/`flags`/`review_notes`.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE assessments SET review_validation_json = ?, "
            "review_validation_at = datetime('now'), "
            "review_validation_edited_at = ? WHERE assessment_id = ?",
            (findings_json, edited_at_snapshot, assessment_id),
        )


def finalize_assessment(assessment_id: int, *, rejected: bool = False) -> str:
    """The only writer of a terminal state (ADR 0007). Reads `edited_at` to
    pick `validated_ok` vs `validated_with_changes`; `rejected=True`
    overrides both. Stamps `finalized_at`. Refuses (raises) on an
    already-terminal row. Returns the chosen state.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, edited_at FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no assessment {assessment_id}")
        if row["status"] in _TERMINAL_STATES:
            raise ValueError(
                f"assessment {assessment_id} is already terminal: {row['status']!r}"
            )

        if rejected:
            new_status = "rejected"
        elif row["edited_at"] is not None:
            new_status = "validated_with_changes"
        else:
            new_status = "validated_ok"

        conn.execute(
            "UPDATE assessments SET status = ?, finalized_at = datetime('now') "
            "WHERE assessment_id = ?",
            (new_status, assessment_id),
        )
    return new_status


def delete_assessments(assessment_ids: list[int]) -> None:
    """Delete `assessments` rows (cascading to `assessment_metrics`/
    `subtest_scores`) plus every file this assessment alone owns: its
    per-page PNG under `PAGES_DIR` and the triage/extraction/review-
    validation JSON `extract_sheet`/`run_review_validations` wrote under
    `RUNS_DIR` for that page.

    Never touches `source_files`/the stored source PDF or another
    assessment's page: `page_image_path` and the run outputs derived from
    it are one-per-`source_page_key` (unique), so deleting them here can't
    remove a file another assessment still depends on.

    Best-effort on the filesystem side — a missing/already-gone file is not
    an error, only a DB row failing to delete is.
    """
    for assessment_id in assessment_ids:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT page_image_path FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if row is None:
                continue
            page_image_path = row["page_image_path"]
            conn.execute(
                "DELETE FROM assessments WHERE assessment_id = ?", (assessment_id,)
            )
        _delete_assessment_files(assessment_id, page_image_path)


def _delete_assessment_files(assessment_id: int, page_image_path: str | None) -> None:
    if page_image_path:
        page_path = Path(page_image_path)
        try:
            page_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "failed to delete file %s for assessment %s", page_path, assessment_id
            )
        clear_run_outputs(page_path.stem)

    delete_review_validation_run_dir(assessment_id)


def clear_run_outputs(page_key: str) -> None:
    """Unlink `RUNS_DIR/<page_key>.json` and `<page_key>.triage.json`.
    Best-effort: a missing file is fine, an `OSError` is logged, never
    raised. Re-extract (ADR 0016 §3) calls this before extracting, so a run
    that writes nothing fails loudly instead of re-reading the stale file.
    """
    for path in (
        config.RUNS_DIR / f"{page_key}.json",
        config.RUNS_DIR / f"{page_key}.triage.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to delete run output %s", path)


def delete_review_validation_run_dir(assessment_id: int) -> None:
    """Remove `RUNS_DIR/review_validation_<assessment_id>`. Best-effort."""
    run_dir = config.RUNS_DIR / f"review_validation_{assessment_id}"
    shutil.rmtree(run_dir, ignore_errors=True)
