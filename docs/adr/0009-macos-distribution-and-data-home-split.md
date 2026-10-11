# ADR 0009 — macOS distribution: code/data split, `~/.demat` home, and a generated `.app`

- Status: Accepted; §2, §3, §6, §7 amended by ADR 0017 (Homebrew replaces the tarball and
  `install.sh`)
- Date: 2026-09-20
- Extends: ADR 0001 (salt lives in the data home), ADR 0002 (PDF rasterizer), ADR 0004
  (runner selection), ADR 0005 §9.6 (external knowledge base)

## Context

Until now the app has only ever run from a developer checkout: `make run-dev`, a git
working tree, `data/` next to the code, the `claude` CLI already installed and
authenticated. The new requirement is that a non-developer can download an archive, run
one install step on their own Mac, and afterwards start the app by double-clicking an
entry in Applications.

Five properties of the current code decide the shape of the answer:

- **`demat/config.py` is the single choke point for paths.** `DATA_DIR`, `DB_PATH`,
  `PAGES_DIR`, `RUNS_DIR`, `UPLOADS_DIR`, `SALT_PATH` and the default
  `KNOWLEDGE_BASE_DIR` all derive from `REPO_ROOT`, which is derived from
  `__file__`. So user data currently lives *inside* the code tree. An upgrade that
  replaces the code tree would destroy the database and the salt.
- **`SKILLS_DIR`, `SCHEMA_SQL_PATH`, `TEST_DEFINITIONS_DIR` are code assets**, not user
  data. They version with the installed release and must stay next to the code.
- **The project is a uv *virtual* project.** `pyproject.toml` has no `[build-system]`
  table and `uv.lock` records `source = { virtual = "." }`. `uv sync` therefore installs
  the dependencies but never builds or installs `demat` itself; `.venv/bin/` contains no
  project console script. Everything works today because `streamlit run app.py` and
  pytest each put the repo root on `sys.path`.
- **The two runners have very different prerequisites.** `ClaudeAgentSdkRunner`
  (current default) needs the `claude` CLI, a Node runtime, and an *interactive
  authentication* on the target machine. `DirectMessagesRunner` needs only
  `ANTHROPIC_API_KEY` and the `anthropic` SDK, which is already a declared dependency.
- **PyMuPDF is AGPL-licensed.** `README.md` already states the swap to `pypdfium2` is a
  precondition for distributing the app, and `demat/pdf_render.py` is deliberately
  isolated so the swap is a one-file change. "Distributing the app" is exactly what this
  ADR is about, so the condition has come due.

Two premises that seemed load-bearing turned out not to be, and are recorded here so
they are not re-litigated:

- `config.ensure_skills_discoverable()` writes into `CODE_ROOT/.claude/skills/`, and
  `agent_runner.py` passes `cwd=str(REPO_ROOT)` to the Claude CLI. Both are reached
  **only** from `ClaudeAgentSdkRunner.run` (`agent_runner.py:122`, `:148`).
  `DirectMessagesRunner` reads `config.SKILLS_DIR/<skill>/SKILL.md` directly and writes
  nothing into the code tree. With `direct` as the distributed default, nothing writes
  to the code root at runtime.
- The knowledge base is **not in the repository** — `data/demat-knowledge-base/` is
  gitignored and holds only a stub. An installer cannot ship it, and does not need to:
  `config.py` documents absence as a normal state and `tests/test_kb_consistency.py`
  skips when it is missing.

## Decision

### 1. Split the code root from a `DEMAT_HOME` data home; default `~/.demat`

`demat/config.py` grows a second root. `CODE_ROOT` (the existing `REPO_ROOT`, kept as a
deprecated alias for one release) keeps owning read-only code assets. A new
`DEMAT_HOME`, resolved as `$DEMAT_HOME` or `~/.demat`, owns everything the user
accumulates:

```
~/.demat/
  app/                     installed payload (code + uv-managed .venv)
  data/                    demat.db, pages/, runs/, uploads/, .salt,
                           demat-knowledge-base/ (externally-provisioned KB,
                           ADR 0005 §9.6)
  logs/                    launcher + app logs
  config.env               DEMAT_RUNNER, ANTHROPIC_API_KEY, DEMAT_PORT (mode 0600)
```

The name is `~/.demat`, matching the package, the repo and `demat.db`. `~/.dermat-plus`
was the other candidate — it is reachable from the "DEMAT+" product wordmark in the UI
(`st.set_page_config(page_title="DEMAT+")`), but the `r` makes it read as a typo of the
package name and creates a third spelling of the product inside one machine. Consistency
with the code wins; a rename later is an installer-only change because every consumer
goes through `config`.

The knowledge base stays a child of `data/` — `DATA_DIR/demat-knowledge-base`, now
resolved from `DEMAT_HOME` instead of `CODE_ROOT` — alongside the app's other resources
(db, salt, pages, runs, uploads, reports). It is externally maintained content that must
never be caught by a reset or a "clear my data" action, but that guarantee comes from
`scripts/reset_data.py`'s explicit enumerated file/dir allowlist, never a blanket `rm -rf
data/`, not from directory placement — so nesting the KB under `data/` alongside
everything else `DEMAT_HOME` owns is safe. The `DEMAT_KB_DIR` override is unchanged.

Consequence: `make run-dev` and an installed launcher share one `~/.demat` by default.
For a single-user machine that is the desired behavior (test what you ship, one
database). Developers who want separation set `DEMAT_HOME` in their shell.

### 2. Payload in `~/.demat/app/`, launcher in `~/Applications/`, nothing in `/Applications`

The installed code lives in `~/.demat/app/`, not `/Applications/DEMAT+.app/Contents/`.
The reason is `uv`: `uv sync` materializes `.venv/` next to `pyproject.toml`, so the
payload directory must be user-writable for install *and* for every upgrade. A
`/Applications` payload would need admin rights on every operation, and if the user ever
opts back into `DEMAT_RUNNER=agent_sdk`, `ensure_skills_discoverable()` would additionally
need write access there.

`~/Applications/DEMAT+.app` is a thin launcher bundle containing an `Info.plist` and one
shell script. `~/Applications` is a real per-user Applications folder that Finder and
Spotlight index; it needs no admin rights.

### 3. Generate the `.app` at install time; never ship a prebuilt bundle

`install.sh` writes the bundle on the target machine. A bundle downloaded through a
browser carries the `com.apple.quarantine` extended attribute, and an unsigned,
un-notarized quarantined bundle is blocked by Gatekeeper with a dead-end dialog. A
bundle created locally by a script the user ran from Terminal is not quarantined and
launches normally. This buys a working double-click without a Developer ID certificate,
notarization, or teaching the user the right-click → Open ritual. The cost — the user
must run one Terminal command once, to install — is accepted: any unsigned distribution
requires an equivalent gesture somewhere, and doing it at install time is the moment
where it is least confusing.

### 4. Distributed installs default to `DEMAT_RUNNER=direct`

The Agent SDK runner cannot be part of a non-developer install: it requires Node, the
`claude` CLI, and an interactive login. `DirectMessagesRunner` needs one secret. The API
surface it uses (`client.messages.parse(output_config=..., output_format=...)` and
`ParsedMessage.parsed_output`) was verified present in the pinned `anthropic` 1.7.0, so
this is not a paper path.

The default is set as `DEMAT_RUNNER=direct` in `~/.demat/config.env`, written by the
installer — **not** by changing the in-code default in `config.py`, which stays
`agent_sdk` so developer checkouts behave as before. The installer prompts for
`ANTHROPIC_API_KEY` and stores it in the same file at mode 0600. The key is the end
user's own; no key is ever baked into a release artifact. ADR 0004's "keep the two
runners symmetric" still holds — this changes configuration, not code paths.

### 5. Swap PyMuPDF for pypdfium2 now, in this change

`pymupdf` is dropped from `[project.dependencies]` and `demat/pdf_render.py` is rewritten
against `pypdfium2` (BSD-3/Apache-2.0). Deferring would mean shipping an AGPL binary to a
third party, which is the exact situation the README flagged.

The non-obvious part for whoever implements it: `page_key` is derived from
`file_sha256 + page_index`, **not** from pixel content (`pdf_render.py:23-24`), so
already-rendered PNGs in `data/pages/` stay valid and no re-extraction is triggered by
the swap. Rendered pixels may differ slightly between the two engines; nothing in the
schema or dedup logic depends on them.

### 6. Keep the virtual project: the launcher runs `uv run --directory`, not a console script

The obvious move is `[project.scripts] demat = "demat.cli:main"`. It does not work here,
and the reason is worth writing down because it is invisible until it fails: console
scripts are materialized by the *wheel install* step, and this project has no
`[build-system]`, so uv never builds or installs it (`source = { virtual = "." }` in
`uv.lock`). Adding a build backend to get the script would also make `CODE_ROOT`
(`Path(__file__).resolve().parent.parent`) depend on whether uv installs the root project
editable or as a wheel — a wheel install puts `config.py` in `site-packages/`, which
makes `CODE_ROOT`, `SKILLS_DIR`, `SCHEMA_SQL_PATH`, `TEST_DEFINITIONS_DIR` and `app.py`
all resolve to nothing. `app.py` is not even inside the `demat` package, so it would not
be in a wheel at all.

So the project stays virtual, and the launcher's last line is

```
exec "$UV" run --directory "$DEMAT_HOME/app" streamlit run app.py \
     --server.port "$PORT" --server.address 127.0.0.1 --server.headless true
```

`uv run --directory` sets the working directory, which is what makes Streamlit find
`.streamlit/config.toml` — the file carrying the theme *and* `showErrorDetails = "none"`.
Without it, a Finder-launched app would show an unthemed UI with Python tracebacks in the
browser. `--server.headless true` is required because the launcher, not Streamlit, decides
when to open the browser (§7). The installer's smoke test uses a small
`scripts/check_install.py` run the same way, instead of a `demat --check` subcommand.

This is what the requirement meant by "an entry point *or an equivalent*": a single
invocation the launcher can make that does not depend on the caller's cwd.

### 7. The launcher resolves its own PATH, probes the port, and logs

A GUI-launched process inherits a minimal PATH with no `/opt/homebrew/bin` and no
`~/.local/bin`, so the launcher locates `uv` by explicit candidate paths. It appends to
`~/.demat/logs/`, health-probes the port before starting anything (double-click twice →
one server, second click just opens the browser), and opens the browser only once the
port answers. Failures surface as an `osascript` alert naming the log file rather than a
silent Dock bounce.

## Consequences

- Upgrades become safe: replacing `~/.demat/app/` cannot touch `~/.demat/data/`.
- Existing developer checkouts need a one-time move of `data/demat.db` **and `data/.salt`
  together**. Losing the salt silently re-derives every `person_id` (ADR 0001) — the
  migration must move both or neither, and the spec makes that a single scripted step.
- `scripts/reset_data.py` currently re-derives `REPO_ROOT`/`DATA_DIR` itself instead of
  importing `config`; after the split it would clear an empty repo-local directory and
  report success. It must import from `demat.config` — and because the project is virtual,
  it must be invoked in a way that puts the project root on `sys.path` (`python -m
  scripts.reset_data` from the project root, not `python scripts/reset_data.py`).
- Tests must not write into a real `~/.demat`. Isolation moves to a `tests/conftest.py`
  that sets `DEMAT_HOME` to a temp dir **before `demat.config` is imported** (`tests/
  __init__.py` is empty and must stay that way, since it is imported first); the
  existing per-fixture `monkeypatch.setattr(config, "DATA_DIR", ...)` layers stay as a
  second line of defense. `DEMAT_KB_DIR` is deliberately *not* clobbered, so the
  knowledge-base consistency tests keep running on a machine that has the KB.
- The knowledge base is not installed by the installer. It creates the directory empty;
  provisioning it stays a documented manual step.
