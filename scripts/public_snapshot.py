#!/usr/bin/env python3
"""Write the tree that gets published to `agherasim/demat-plus-public`
(ADR 0017) into an empty directory.

The tree is `git archive HEAD` restricted to exactly the files
`scripts/check_kb_leak.py` scans (its `tracked_files()`: tracked files minus
`.public-exclude`), so what was checked and what is published are the same
list by construction. Requires a clean checkout, so the index is HEAD. The
file list is asserted after extraction, and the run fails on any difference.

    uv run python -m scripts.public_snapshot --out <empty dir>
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
from pathlib import Path

from scripts.check_kb_leak import tracked_files


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True
    ).stdout


def write_snapshot(root: Path, out: Path) -> list[str]:
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("error: checkout has uncommitted changes")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"error: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    expected = sorted(tracked_files(root))
    # Literal: the names come from ls-tree, not from a human-written pathspec.
    archive = _git(
        root, "--literal-pathspecs", "archive", "--format=tar", "HEAD", "--", *expected
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(out, filter="tar")
    written = sorted(
        str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() or p.is_symlink()
    )
    if written != expected:
        missing = sorted(set(expected) - set(written))
        extra = sorted(set(written) - set(expected))
        raise SystemExit(f"error: snapshot mismatch; missing={missing} extra={extra}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    files = write_snapshot(args.root.resolve(), args.out)
    print(f"wrote {len(files)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
