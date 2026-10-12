"""Central configuration: paths, model id, runner selection, salt resolution."""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

# --- Paths -----------------------------------------------------------------

CODE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_ROOT  # deprecated alias; remove after one release

# Data home: everything the user accumulates (DB, salt, rendered pages,
# per-run output, the knowledge base, logs). Kept separate from `CODE_ROOT`
# so an upgrade that replaces the installed code can never touch user data —
# see ADR 0009. No implicit default: a missing or mistyped `DEMAT_HOME` must
# fail loudly, not silently stand up a fresh, empty data store somewhere the
# user didn't intend (e.g. `~/.demat`) while their real data sits untouched
# elsewhere — that failure mode is exactly what motivated this check. The
# `demat` command (`packaging/demat`, ADR 0017) and `tests/conftest
# .py` always export `DEMAT_HOME` (and create the directory) before this
# module is imported; a developer checkout must do the same.
_DEMAT_HOME_ENV = os.environ.get("DEMAT_HOME")
if not _DEMAT_HOME_ENV:
    raise RuntimeError(
        "DEMAT_HOME is not set. This app has no default data home — set the "
        "DEMAT_HOME environment variable to the directory holding (or that "
        "should hold) demat.db, pages/, runs/, uploads/, and the knowledge "
        "base, then re-run."
    )
DEMAT_HOME = Path(_DEMAT_HOME_ENV).expanduser()
if not DEMAT_HOME.is_dir():
    raise RuntimeError(
        f"DEMAT_HOME={DEMAT_HOME} does not exist (or is not a directory). It "
        "is not created automatically — create it first, or point DEMAT_HOME "
        "at the correct existing data home."
    )

DATA_DIR = DEMAT_HOME / "data"
DB_PATH = DATA_DIR / "demat.db"
PAGES_DIR = DATA_DIR / "pages"
RUNS_DIR = DATA_DIR / "runs"
UPLOADS_DIR = DATA_DIR / "uploads"
REPORTS_DIR = DATA_DIR / "reports"
SALT_PATH = DATA_DIR / ".salt"
LOGS_DIR = DEMAT_HOME / "logs"

SKILLS_DIR = CODE_ROOT / "demat" / "skills"
SCHEMA_SQL_PATH = CODE_ROOT / "demat" / "schema.sql"
TEST_DEFINITIONS_DIR = CODE_ROOT / "demat" / "test_definitions"

# External DEMAT knowledge base (data/norm tables, manual excerpts). An
# externally maintained, swappable dependency — see ADR 0005 §9.6. This is
# the *only* place its path appears; everything else reads this value.
# Absent is a normal state: definitions load, the app starts and extraction
# runs fine with no knowledge base present. A child of `data/`, alongside the
# other resources the app loads (ADR 0009 update): `scripts/reset_data.py`
# protects it from a reset via its own explicit file/dir allowlist, not via
# directory nesting, so co-locating it under `data/` is safe.
KNOWLEDGE_BASE_DIR = Path(
    os.environ.get("DEMAT_KB_DIR") or (DATA_DIR / "demat-knowledge-base")
)

# The Claude Code CLI discovers project skills under `.claude/skills/`, not an
# arbitrary project path. Skills are authored/tracked under `demat/skills/`
# (per spec) and made discoverable via one symlink per skill at
# `.claude/skills/<name>`, created idempotently by
# `ensure_skills_discoverable()`. `EXTERNAL_SKILL_DIRS` is a hook for skills
# living outside the repo (e.g. the knowledge base) — ships empty; see ADR
# 0005 §9.7. Whether adding to it is safe depends on `ClaudeAgentOptions
# .skills` being an allowlist (confirmed — see the implementation report),
# but the entry itself is still deferred to its own change.
#
# Both live under `AGENT_WORKDIR`, the CLI's working directory: a dedicated,
# otherwise empty dir in the data home, not `CODE_ROOT` (a Homebrew install's
# code dir is the Cellar, which must stay as installed) and not `DEMAT_HOME`
# itself (that would put the DB, salt and uploads in the agent's reach; it
# only gets `PAGES_DIR`/`RUNS_DIR` via `add_dirs`). ADR 0017 §8.
AGENT_WORKDIR = DEMAT_HOME.resolve() / "agent"
CLAUDE_DIR = AGENT_WORKDIR / ".claude"
CLAUDE_SKILLS_LINK = CLAUDE_DIR / "skills"
EXTERNAL_SKILL_DIRS: list[Path] = []

# `ensure_skills_discoverable` unlinks/relinks `.claude/skills/<name>`; guard
# it so concurrent extraction workers (see `pipeline.process_pdfs`) can't
# race each other's symlink swap.
_skills_discoverable_lock = threading.Lock()

# Streamlit port for the installed launcher (and `make run-dev` if set).
PORT = int(os.environ.get("DEMAT_PORT", "8701"))


def ensure_data_dirs() -> None:
    # DEMAT_HOME itself is required to already exist (see the check above);
    # only its subdirectories are created on demand here.
    DEMAT_HOME.chmod(0o700)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _skill_frontmatter_name(skill_md_path: Path) -> str | None:
    """Best-effort extraction of the `name:` field from a SKILL.md's YAML
    frontmatter, without a YAML dependency (frontmatter here is simple
    `key: value` lines).
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def ensure_skills_discoverable() -> None:
    """Make every skill in `SKILLS_DIR` and `EXTERNAL_SKILL_DIRS` visible to
    the Claude Code CLI's skill discovery.

    The CLI looks for project skills under `.claude/skills/<name>/SKILL.md`.
    This project authors in-repo skills under `demat/skills/` instead (single
    source of truth, see spec/ADR 0004), so `.claude/skills/` is generated as
    a real directory containing one symlink per skill, named by that skill's
    frontmatter `name`, pointing at the skill's absolute path (the links live
    under `AGENT_WORKDIR`, which need not be inside `CODE_ROOT`). Idempotent;
    safe to call repeatedly.

    A previous version of this function created `.claude/skills` itself as a
    single symlink to `demat/skills`. If that symlink is still there, it is
    unlinked first — an early-return on `.exists()` would otherwise keep the
    old single-symlink layout forever.
    """
    with _skills_discoverable_lock:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

        if CLAUDE_SKILLS_LINK.is_symlink():
            CLAUDE_SKILLS_LINK.unlink()
        CLAUDE_SKILLS_LINK.mkdir(parents=True, exist_ok=True)

        skill_dirs = [
            d for d in sorted(SKILLS_DIR.iterdir()) if d.is_dir()
        ] + EXTERNAL_SKILL_DIRS

        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            name = _skill_frontmatter_name(skill_md) or skill_dir.name
            link_path = CLAUDE_SKILLS_LINK / name
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(skill_dir.resolve(), target_is_directory=True)


# --- Model / runner ----------------------------------------------------------

MODEL_ID = os.environ.get("DEMAT_MODEL_ID", "claude-opus-5")

# "agent_sdk" (default) | "direct"
RUNNER = os.environ.get("DEMAT_RUNNER", "agent_sdk")

# Upper bound on concurrent sheet-extraction workers, shared across every
# page of every PDF in a batch (not a per-file limit) — see
# `pipeline.process_pdfs`.
MAX_PARALLEL_WORKERS = max(1, int(os.environ.get("DEMAT_MAX_PARALLEL_WORKERS", "4")))

DPI = 200
MAX_IMAGE_EDGE_PX = 1568

# Store raw handwritten names in `person_names` (used only to resolve flagged
# ID collisions). Flip to False for strict pseudonymization.
STORE_RAW_NAMES = os.environ.get("DEMAT_STORE_RAW_NAMES", "true").lower() not in (
    "0",
    "false",
    "no",
)


# --- Salt resolution ---------------------------------------------------------


def resolve_id_salt() -> str:
    """Resolve the per-install salt used to key the pseudonymous ID hash.

    First hit wins: `st.secrets["demat"]["id_salt"]`, then `DEMAT_ID_SALT`,
    then an auto-generated value persisted to `SALT_PATH` (mode 0600).
    """
    try:
        import streamlit as st

        salt = st.secrets["demat"]["id_salt"]
        if salt:
            return str(salt)
    except Exception:
        # No secrets.toml, no [demat] section, or no Streamlit context.
        pass

    env_salt = os.environ.get("DEMAT_ID_SALT")
    if env_salt:
        return env_salt

    ensure_data_dirs()
    if SALT_PATH.exists():
        return SALT_PATH.read_text().strip()

    new_salt = secrets.token_urlsafe(32)
    SALT_PATH.write_text(new_salt)
    SALT_PATH.chmod(0o600)
    return new_salt
