"""Test isolation: point `DEMAT_HOME` at a throwaway directory before any
test module imports `demat.config`, so `make test` never reads or writes a
real `~/.demat` (ADR 0009).

The ordering is the whole trick: this must happen at *module top level*,
before the `os.environ["DEMAT_HOME"]` assignment below, and before any
`import demat...`. pytest imports `tests/__init__.py` before this file, so
that module must stay empty (see the comment there).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["DEMAT_HOME"] = tempfile.mkdtemp(prefix="demat-test-home-")

# Keep the real KB visible if the developer has one, so the KB consistency
# tests don't silently start skipping on the machine where they matter.
if "DEMAT_KB_DIR" not in os.environ:
    _repo_kb = Path(__file__).resolve().parent.parent / "data" / "demat-knowledge-base"
    _home_kb = Path.home() / ".demat" / "data" / "demat-knowledge-base"
    for _cand in (_home_kb, _repo_kb):
        if (_cand / "SKILL.md").exists():
            os.environ["DEMAT_KB_DIR"] = str(_cand)
            break

# Only after this point may anything import demat.* — importing `config`
# here would fix its module-level constants before the env vars above take
# effect.
import pytest

from demat import config


@pytest.fixture(autouse=True, scope="session")
def _assert_test_home_is_not_real_home() -> None:
    """Fail loudly if the DEMAT_HOME-before-import ordering ever breaks."""
    assert config.DEMAT_HOME != Path.home() / ".demat", (
        "config.DEMAT_HOME resolved to the real ~/.demat during tests — "
        "the DEMAT_HOME env var must be set before demat.config is imported "
        "(see the module-level code at the top of this file)."
    )
