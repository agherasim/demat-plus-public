#!/usr/bin/env python3
"""Destructive reset for the ADR 0007 schema change (no migration).

Deletes `DEMAT_HOME/data/demat.db` (+ `-wal`/`-shm`) and clears the contents
of `data/pages/` and `data/runs/`. Leaves `data/.salt` untouched. The
knowledge base lives at `data/demat-knowledge-base/` (ADR 0009) but is never
touched by this script: an explicit enumerated path list, never `rm -rf
data/`, is what keeps it (and `data/uploads/`, `data/reports/`) out of
reach — not directory nesting. Tolerates paths that don't exist (nothing to
error on a fresh checkout).

Because the project is a uv *virtual* project, `uv run python
scripts/reset_data.py` does not put the project root on `sys.path`. Run
this as a module, from the project root, instead:

    uv run python -m scripts.reset_data --yes
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from demat import config

DB_FILES = [
    config.DB_PATH,
    config.DATA_DIR / "demat.db-wal",
    config.DATA_DIR / "demat.db-shm",
]

# Directories whose *contents* are cleared, but which are left in place
# (recreated empty by `config.ensure_data_dirs()` on next app start).
CLEARED_DIRS = [
    config.PAGES_DIR,
    config.RUNS_DIR,
]


def _remove_file(path: Path) -> None:
    if path.exists():
        path.unlink()
        print(f"removed {path}")


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        print(f"removed {child}")


def reset() -> None:
    for db_file in DB_FILES:
        _remove_file(db_file)
    for cleared_dir in CLEARED_DIRS:
        _clear_dir(cleared_dir)
    print(f"done. {config.SALT_PATH} left untouched.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive reset (required)",
    )
    args = parser.parse_args()

    print(f"DEMAT_HOME: {config.DEMAT_HOME}")
    print(f"DATA_DIR:   {config.DATA_DIR}")

    if not args.yes:
        print(
            f"This deletes {config.DB_PATH}(+wal/shm) and clears "
            f"{config.PAGES_DIR} and {config.RUNS_DIR}. Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1

    reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
