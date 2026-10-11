# AGENTS.md

Streamlit app that turns scanned Hogrefe DEMAT score sheets into structured, queryable
records in a local SQLite database. Full context: `README.md` and `docs/adr/` (one ADR
per major design decision — read the relevant one before changing that area).

## Commands

```
make run-dev   # uv run streamlit run app.py
make test      # uv run pytest
make lint      # codespell, ruff check, ruff format --check, mypy
```

Run a single test file/case with `uv run pytest tests/test_validation.py -k <name>`.

Before committing, run `make lint` and fix everything it reports — don't hand-fix around
it. `ruff format .` and `uv run ruff check . --fix` resolve most issues automatically.

## Workflow

- Always do feature/bug work in a separate git worktree, never directly on `main` in the
  primary checkout — e.g. `git worktree add -b feat/<slug> ../demat-worktrees/feat-<slug>
  main`. Never implement on `main`. `.env` and `data/` are gitignored and not brought over
  by `git worktree add` — copy both from the primary checkout into the new worktree, or the
  app starts there with no `DEMAT_HOME` config and an empty database.
- When the work merges or is abandoned, always clean up: `git worktree remove
  ../demat-worktrees/feat-<slug>` and delete the branch locally (`git branch -d
  feat/<slug>`) — don't leave stale worktrees/branches lying around.

## Code layout

- `app.py` — the single Streamlit entrypoint (all pages/tabs live here; Streamlit reruns
  this script on every widget interaction, so keep state in `st.session_state`).
- `demat/` — the package: `pipeline.py` (orchestration), `extraction.py` (agent
  triage/extraction + retry), `agent_runner.py` (the two runner backends), `store.py`
  (SQLite access), `schema.py` (Pydantic models for extracted data), `test_registry.py`
  (loads `demat/test_definitions/*.toml`), `validation.py`, `identity.py`, `pdf_render.py`.
- `demat/test_definitions/*.toml` — per-DEMAT-version subtest/metric tables (ADR 0005).
  Adding a DEMAT version is data, not code: add a TOML file, not a new code path.
- `demat/skills/` — Agent Skill definitions (one dir per DEMAT version, plus triage and a
  generic fallback), the source of truth for extraction instructions under both runners.
  Edit `demat/skills/_source/reference.md` then run `uv run python scripts/sync_skills.py`
  to regenerate per-skill copies — never hand-edit the generated `reference.md` copies.
- `docs/adr/` — one file per accepted decision, later ones may amend/extend earlier ones
  (check the `Status`/`Amends`/`Extends` header). Add a new ADR for any non-trivial design
  decision rather than only relying on commit messages.
- `~/.demat/data/` (`config.DATA_DIR`, default `$DEMAT_HOME/data`) — SQLite DB, rendered
  page PNGs, per-run agent JSON, uploaded PDFs, the identity salt. Lives outside the repo
  entirely (ADR 0009), not just gitignored; never assume it's present in a fresh clone.

## Conventions

- Python >= 3.13, dependencies/lockfile managed by `uv` (`pyproject.toml` / `uv.lock`).
- Type-annotate all non-test code; `demat/` and `app.py` are checked under mypy strict-ish
  settings (see `[tool.mypy]` in `pyproject.toml`). Tests are exempt from
  `disallow_untyped_defs`/`arg-type` — don't fight mypy there, annotate real modules.
  PEP 695 generic syntax (`def f[T](...)`) is the preferred style, not `TypeVar`.
  Explicitly type kwargs unpacked from a `**dict` and other spots mypy calls out.
- Ruff (`select = ["E","F","W","I","C","B","UP","RUF"]`, line-length 88) is the source of
  truth for formatting and import order — run `ruff format` instead of hand-formatting.
- German domain terms (DEMAT is a German math test — "Geometrie", umlaut transliterations
  like "ue"/"Ue") are expected in code, tests, and TOML defs; extend
  `[tool.codespell] ignore-words-list` rather than "fixing" them if codespell flags a new
  one.
- Nothing from the DEMAT knowledge base may be committed — no manual prose, no norm-table
  values, not even one RW→PR lookup or band edge in a test fixture (ADR 0017). This repo is
  mirrored to the public `agherasim/demat-plus-public`; CI's `kb-guard` job
  (`scripts/check_kb_leak.py`) blocks the sync if it finds any. Use fabricated values in
  tests; tests that need the real values belong in `demat-knowledge-base/tests/`. Paths
  listed in `.public-exclude` are never published.
- Handwritten child names and any other PII must never leave `data/` (gitignored) or the
  `person_names` table (see ADR 0001/0006) — do not add logging, exports, or UI views that
  surface raw names outside what those ADRs already scope.
- No dead runner/version branches: the two extraction runners
  (`ClaudeAgentSdkRunner`/`DirectMessagesRunner`) and the four DEMAT versions are meant to
  stay symmetric — if you add a code path for one, check whether the others need it too.
- Only `demat/config.py` derives filesystem paths from `__file__` or resolves
  `DEMAT_HOME`/`CODE_ROOT`. Every other module reads paths from `config` — don't
  re-derive `REPO_ROOT`/`DATA_DIR`/etc. locally (ADR 0009; `scripts/reset_data.py` used to
  do this and it was a bug).
