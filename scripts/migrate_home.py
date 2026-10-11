#!/usr/bin/env python3
"""One-time move of an existing developer checkout's repo-local `data/` into
`$DEMAT_HOME` (ADR 0009).

Before this change, `demat/config.py` derived `DATA_DIR` from the repo root,
so a developer checkout may still have a live `data/demat.db`, `data/.salt`,
and rendered pages/runs/uploads sitting next to the code. This script moves
those, plus a repo-local `data/demat-knowledge-base/` if present, into the
new data home (default `~/.demat`, or `$DEMAT_HOME` if set).

The database and the salt move together or not at all: a database meeting a
different salt silently re-derives every `person_id` (ADR 0001), so if the
destination already holds either `demat.db` or `.salt`, this script refuses
outright rather than guessing which one should win.

Uses an explicit enumerated path list, never `mv data/*`. Nothing is touched
until `--yes` is passed; run without it first to see exactly what would move.

Invocation (project is a uv virtual project, so `-m` is required to put the
project root on `sys.path`):

    uv run python -m scripts.migrate_home --yes
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from demat import config


def _legacy_data_dir() -> Path:
    return config.CODE_ROOT / "data"


def _legacy_files() -> list[tuple[Path, Path]]:
    """(source, destination) pairs for files that move individually.

    Computed fresh from `config` on every call (rather than fixed at import
    time) so overriding `DEMAT_HOME`/`CODE_ROOT` — as tests do — is honored.
    """
    legacy = _legacy_data_dir()
    return [
        (legacy / "demat.db", config.DB_PATH),
        (legacy / "demat.db-wal", config.DATA_DIR / "demat.db-wal"),
        (legacy / "demat.db-shm", config.DATA_DIR / "demat.db-shm"),
        (legacy / ".salt", config.SALT_PATH),
    ]


def _legacy_dirs() -> list[tuple[Path, Path]]:
    """(source, destination) pairs for directories that move wholesale.

    Computed fresh from `config` on every call; see `_legacy_files()`.
    """
    legacy = _legacy_data_dir()
    return [
        (legacy / "pages", config.PAGES_DIR),
        (legacy / "runs", config.RUNS_DIR),
        (legacy / "uploads", config.UPLOADS_DIR),
        (legacy / "demat-knowledge-base", config.KNOWLEDGE_BASE_DIR),
    ]


def _guard_db_and_salt_move_together() -> str | None:
    """Return an error message if the DB/salt pairing would be violated."""
    legacy_db = _legacy_data_dir() / "demat.db"
    legacy_salt = _legacy_data_dir() / ".salt"
    dest_db_exists = config.DB_PATH.exists()
    dest_salt_exists = config.SALT_PATH.exists()

    if legacy_db.exists() and dest_salt_exists and not legacy_salt.exists():
        return (
            f"refusing to move {legacy_db}: destination already has a salt "
            f"({config.SALT_PATH}) but the source has none — moving the DB "
            "alone would re-derive every person_id against a different salt."
        )
    if legacy_salt.exists() and dest_db_exists and not legacy_db.exists():
        return (
            f"refusing to move {legacy_salt}: destination already has a "
            f"database ({config.DB_PATH}) but the source has no database — "
            "moving the salt alone would re-derive every person_id against "
            "the existing DB."
        )
    if legacy_db.exists() and dest_db_exists:
        return f"refusing to move {legacy_db}: {config.DB_PATH} already exists."
    if legacy_salt.exists() and dest_salt_exists:
        return f"refusing to move {legacy_salt}: {config.SALT_PATH} already exists."
    return None


def _planned_moves() -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for src, dest in _legacy_files():
        if src.exists():
            moves.append((src, dest))
    for src, dest in _legacy_dirs():
        if src.exists() and any(src.iterdir()):
            moves.append((src, dest))
    return moves


def migrate(moves: list[tuple[Path, Path]]) -> None:
    # Snapshot which destinations already exist *before* `ensure_data_dirs()`
    # mkdirs `PAGES_DIR`/`RUNS_DIR`/`UPLOADS_DIR` — otherwise every directory
    # move would see its own freshly-created (empty) destination and get
    # skipped right after being printed as "planned" (data-loss bug fixed by
    # this snapshot; see the reviewer's repro in the ADR/PR history).
    pre_existing = {dest for _, dest in moves if dest.exists()}
    config.ensure_data_dirs()
    for src, dest in moves:
        if dest in pre_existing:
            print(f"skipping {src} -> {dest}: destination already exists")
            continue
        if dest.exists():
            # `ensure_data_dirs()` created this as an empty directory since
            # the snapshot above; `shutil.move` would move `src` *inside* it
            # (e.g. `pages/pages/`) rather than onto it, so clear it first.
            dest.rmdir()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        print(f"moved {src} -> {dest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the migration (required)",
    )
    args = parser.parse_args()

    print(f"legacy data dir: {_legacy_data_dir()}")
    print(f"DEMAT_HOME:      {config.DEMAT_HOME}")
    print(f"DATA_DIR:        {config.DATA_DIR}")
    print(f"KNOWLEDGE_BASE_DIR: {config.KNOWLEDGE_BASE_DIR}")

    error = _guard_db_and_salt_move_together()
    if error:
        print(error, file=sys.stderr)
        return 1

    moves = _planned_moves()
    if not moves:
        print("nothing to migrate.")
        return 0

    print("planned moves:")
    for src, dest in moves:
        print(f"  {src} -> {dest}")

    if not args.yes:
        print("Re-run with --yes to perform the moves above.", file=sys.stderr)
        return 1

    migrate(moves)
    return 0


if __name__ == "__main__":
    sys.exit(main())
