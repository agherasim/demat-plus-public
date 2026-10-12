# Demat

Streamlit app that turns uploaded scans of handwritten Hogrefe DEMAT score sheets into
structured, queryable records in a local SQLite database. See `docs/adr/` for the design
decisions and `pyproject.toml`/`uv.lock` for dependencies.

## Prerequisites

- Python >= 3.13, managed via `uv`.
- **Default extraction runner (`ClaudeAgentSdkRunner`)** drives the Claude Code CLI via
  the `claude-agent-sdk` package. This requires the `claude` CLI (and its Node.js
  runtime) to be installed and authenticated on the machine running the app. If it is
  missing, the "Extract" button in the "Load Assessments" tab surfaces a clear error
  instead of a traceback, and names the fix.
- **Fallback runner (`DirectMessagesRunner`)**: a plain Anthropic Messages API call,
  usable without the `claude` CLI. Requires `ANTHROPIC_API_KEY` (or another credential
  the `anthropic` SDK resolves) to be set. Select it with the environment variable
  `DEMAT_RUNNER=direct`.
- PDF rasterization uses `pypdfium2` (BSD-3/Apache-2.0), swapped in for the previously
  AGPL-licensed PyMuPDF specifically so this app can be distributed (see
  `docs/adr/0009-macos-distribution-and-data-home-split.md` §5).

## Data and privacy

- All extracted data lives under `$DEMAT_HOME/data/` (SQLite DB, rendered page PNGs,
  per-run agent output JSON, uploaded PDFs). `DEMAT_HOME` defaults to `~/.demat` and is
  never committed to the repo.
- Children are identified by a deterministic 4-character pseudonymous ID (see
  `docs/adr/0001-deterministic-pseudonymous-child-id.md`), derived from a per-install
  salt. The salt resolves, in order: `st.secrets["demat"]["id_salt"]`, the
  `DEMAT_ID_SALT` environment variable, or an auto-generated value persisted to
  `$DEMAT_HOME/data/.salt` (mode `0600`) on first run. IDs are **not** portable between
  installs.
- Raw handwritten names are stored separately (`person_names` table) only to let a human
  resolve a flagged ID collision; they are excluded from the default "Results" view and
  from CSV export. Set `DEMAT_STORE_RAW_NAMES=false` to suppress storing them entirely.

## Running (developer checkout)

```
make run-dev
```

which runs `uv run streamlit run app.py`.

## Install on another Mac

DEMAT+ installs with [Homebrew](https://brew.sh) from the public mirror
[`agherasim/demat-plus-public`](https://github.com/agherasim/demat-plus-public) (ADR 0017):

```
brew install agherasim/tap/demat
demat setup
```

`demat setup` runs once. It:

- creates `~/.demat/`;
- prompts for an `ANTHROPIC_API_KEY` (input hidden, stored in `~/.demat/config.env` at mode
  `0600`);
- installs the Python dependencies into `~/.demat/venv`;
- generates `~/Applications/DEMAT+.app`;
- runs an install check.

Double-click **DEMAT+** in `~/Applications` to start the app, or run `demat run`. The
installed app runs with `DEMAT_RUNNER=direct` (the plain Anthropic Messages API) using your
own API key, so it does not need the `claude` CLI. Pass `demat setup --non-interactive` to
skip the key prompt and add the key to `config.env` later.

If you already use Claude Code, run `demat setup --runner agent_sdk` instead. That runner
uses the `claude` CLI and its login, so there is no API key to enter. Running it again on an
existing install only switches `DEMAT_RUNNER` in `config.env`. The launcher finds `claude`
in `/opt/homebrew/bin`, `/usr/local/bin` or `~/.local/bin`.

The first setup needs network access: it downloads a managed CPython 3.13 and the
dependencies.

### The DEMAT knowledge base (separate, private)

The knowledge base holds copyrighted manual content. It is **never** part of this repo, its
public mirror, or the Homebrew formula. If you have access to the private repository,
install it by hand:

```
git clone git@github.com:agherasim/demat-knowledge-base.git ~/.demat/data/demat-knowledge-base
```

`DEMAT_KB_DIR` overrides the location. The app runs without the knowledge base; norm
completion and the KB-backed checks are then unavailable.

## Upgrade

```
brew upgrade demat
```

Your data, salt, knowledge base and `config.env` are never touched. The next start re-syncs
`~/.demat/venv` to the new version's lockfile.

If you installed an earlier version from a release tarball (`install.sh`), run
`demat setup` once after `brew install`. It removes the old `~/.demat/app*` code
directories and leaves `~/.demat/data` alone.

## Uninstall

```
demat uninstall-app
brew uninstall demat
```

This removes the launcher and the code. `~/.demat` (data, logs, `config.env`, `venv`, the
knowledge base) stays. Delete it by hand only if you mean to. That is irreversible:
deleting `~/.demat/data/.salt` makes every existing pseudonymous ID unreproducible
(ADR 0001).

## Where your data lives

Everything the app produces or you configure lives under `$DEMAT_HOME` (default
`~/.demat`), separate from the installed code so an upgrade never touches it:

```
~/.demat/
  venv/                    Python environment (re-synced on upgrade)
  data/                    demat.db, pages/, runs/, uploads/, .salt,
                           demat-knowledge-base/ (externally-provisioned, not shipped)
  logs/                    launcher + app logs
  config.env               DEMAT_RUNNER, ANTHROPIC_API_KEY, DEMAT_PORT (mode 0600)
```

`config.env` is `source`d as shell, so if you hand-edit it, quote values yourself too — e.g.
`ANTHROPIC_API_KEY='sk-...'` — especially if a value contains `#`, `$`, spaces, or quotes;
an unquoted value with any of those will break on the next launch.

To back up your data, copy `~/.demat/data/` **including the hidden `.salt` file** — the
DB and salt must travel together, or every pseudonymous ID re-derives to something
different (ADR 0001). `DEMAT_HOME` (and each individual path) can be overridden via
environment variables read in `demat/config.py`.

## Logs

The installed app logs to `~/.demat/logs/launcher.log` (truncated once it exceeds ~1 MB).
It is safe to paste into a bug report: it never contains `config.env`'s contents, API
keys, or child names.

## Skills

Agent Skills live under `demat/skills/` (one directory per DEMAT version, plus a triage
skill and a generic fallback) and are the single source of truth for extraction
instructions, under both runners. The Claude Code CLI discovers project skills under
`.claude/skills/`; on first run, `config.ensure_skills_discoverable()` creates a symlink
`.claude/skills -> ../demat/skills` so the authored skills are visible to the CLI without
duplicating them.

After editing `demat/skills/_source/reference.md` (the shared transcription rules),
run:

```
uv run python scripts/sync_skills.py
```

to regenerate the per-skill `reference.md` copies.
