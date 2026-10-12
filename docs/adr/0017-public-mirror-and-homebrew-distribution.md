# ADR 0017 — Public mirror `demat-plus-public`, a build-time knowledge-base leak guard, and Homebrew as the only installer

- Status: Accepted
- Date: 2026-10-11
- Amends: ADR 0009 §2 (payload location), §3 (generated `.app`), §6 (launcher invocation),
  §7 (launcher PATH) — the tarball + `install.sh` path is replaced by a Homebrew formula
- Extends: ADR 0005 §9.6 (external knowledge base), ADR 0001/0006 (no PII outside `data/`)

## Context

The app is to be installed with `brew install`. The formula lives in the public tap
`agherasim/homebrew-tap`, and Homebrew downloads its source without credentials. So the
source must be public. `agherasim/demat-plus` is private.

The DEMAT knowledge base (`agherasim/demat-knowledge-base`) contains excerpts from the
Hogrefe manuals and their norm tables. That content is copyrighted. **Nothing derived from
it may ever be public.** That rules out whole files, quoted prose, and individual RW → PR/T
lookups or band edges.

A full audit of `demat-plus` before this change found no knowledge-base files, page images,
databases or manual prose anywhere in its history. It did find manual-derived values in some
tests, fixtures and internal docs. Cleaning HEAD would not remove them from the history,
so the existing repo cannot simply be made public.

## Decision

### 1. A separate public repo, fed only by snapshots

`demat-plus` stays private and keeps its full history. A new public repo,
**`agherasim/demat-plus-public`**, receives one commit per sync:

- The commit's tree is `git archive <main sha>` minus the paths in `.public-exclude`.
- Its parent is the previous public commit.
- It is authored by the Actions bot, with the message `Sync demat-plus@<sha>`.

Private history, author emails and excluded paths never reach the public repo. The sync is
never a `git push` of private commits. A CI job does it on every push to `main`, after lint,
tests and the guard have all passed for that exact SHA. A `v*` tag in the private repo is
mirrored as the same tag on the public snapshot. The public repo is the only source the
Homebrew formula references (`url`, `head`).

`.public-exclude` is a list of git pathspecs, read by both the guard and the sync job, so
the guard scans exactly the tree that is published. The first entries are:

- `docs/specs/` and `.agents/`: internal working specs.
- `docs/adr/0014-child-report-examples.md`: example reports that show the manuals' band
  legend.
- `.github/`: the private CI and its deploy-key wiring.

### 2. KB-dependent regression tests live in the KB repo

Some tests need *real* norm values to be worth anything: norm completion against actual
table rows, and exact band edges. These move to `demat-knowledge-base/tests/` and run against
a demat checkout (`DEMAT_CODE_ROOT`). Without the KB they don't run at all, by design.

The demat repo keeps:

- tests that read the KB at runtime but assert only structure (they skip on a public clone);
- pure unit tests, whose values are now fabricated.

### 3. A build-time guard, not a local one

`scripts/check_kb_leak.py` runs in the private repo's CI (`kb-guard` job). The KB is checked
out there through a read-only deploy key and never cached or uploaded. The guard compares the
publishable tree against the KB with structural detectors:

| Detector | What it flags |
|---|---|
| `file` | a file identical to, or named like, a KB data file |
| `prose` | an 8-word run of KB prose |
| `bands` | a cluster of one test's band labels |
| `lookup` | a `(test, skala, normgruppe, rw): {…}` literal holding real values |
| `group` | a real band range shortly after its norm group is named |
| `ranges` | a cluster of real band ranges |
| `pr-list` | a line listing several `PR n` values |

Findings print as `path:line detector`, never the values. Reviewed false positives go into
`.kb-guard-allow` as exact lines, so any edit to an allowed line re-triggers the check. The
guard's own unit tests use a synthetic KB in `tmp_path`.

The guard is a net, not a proof. The detectors were validated against the pre-cleanup tree: they flagged everything the
manual audit had found, and more.

**Merge gating.** The intent is that a PR into `main` cannot merge with a red `ci`. GitHub's
free plan offers no branch protection or rulesets on private repos, so until the account
has GitHub Pro the check is advisory on PRs. The enforced gate is the sync. It re-runs on
the pushed `main` SHA and publishes nothing unless all three checks pass. Bypassing PR
checks (an admin merge, or a direct push) is accepted. Bypassing the sync gate is not
possible from the private repo.

### 4. Homebrew replaces `install.sh`

The tarball, `install.sh`, `uninstall.sh` and `make dist` are removed. The formula `demat`
in `agherasim/homebrew-tap`:

- builds from the public repo's tag archive;
- installs the code into `libexec`;
- depends on `uv`;
- adds a `demat` command.

The rest of ADR 0009 stands: the `~/.demat` data home, the `direct` runner default, and a
local `.app` generated from Terminal (no quarantine). The pieces that change:

- The code lives at the formula's stable `opt_libexec`, not in `~/.demat/app`.
- The Python environment lives at `~/.demat/venv` (`UV_PROJECT_ENVIRONMENT`), outside the
  versioned Cellar, so `brew upgrade` doesn't strand it. The `demat` command re-syncs it
  when the installed version changes.
- `brew install` can neither prompt nor write to `$HOME`. So `demat setup` does that work
  once: it writes `config.env` (API key, `DEMAT_RUNNER=direct`), generates
  `~/Applications/DEMAT+.app`, and runs `scripts/check_install.py`. The formula's `caveats`
  say so.
- An existing tarball install's `~/.demat/app` and `app.prev` are removed by `demat setup`.
  `~/.demat/data` is never touched.

### 5. The knowledge base stays a manual, private install

No formula, resource or tap entry references it. The caveats print the clone command:

```
git clone git@github.com:agherasim/demat-knowledge-base.git ~/.demat/data/demat-knowledge-base
```

Only users with access to that private repo can run it. `DEMAT_KB_DIR` still overrides the
location. The app runs without the KB, and the KB-dependent tests skip.

### 8. Update (2026-10-12): the `agent_sdk` runner is a supported install option

`direct` stays the default. `demat setup --runner agent_sdk` writes `DEMAT_RUNNER=agent_sdk`
and skips the key prompt; on an existing `config.env` it rewrites only that line. Making
the CLI runner work from a Homebrew install needed two changes:

- The CLI's working directory and its `.claude/skills/` links move from `CODE_ROOT` to
  `DEMAT_HOME/agent` (`config.AGENT_WORKDIR`), a directory holding nothing else. The Cellar
  stays exactly as installed. The agent's file access stays what it was: its working
  directory plus `PAGES_DIR`/`RUNS_DIR`, never the DB, salt or uploads. The links are
  absolute and recreated on every run, so a `brew upgrade` repoints them.
- `demat run` unsets `ANTHROPIC_API_KEY` when the runner is `agent_sdk`. Otherwise a key
  left in `config.env` would make the CLI bill the API instead of using its login.
- The `.app` launcher adds `~/.local/bin`, where the Claude Code installer puts `claude`,
  to the PATH.

`scripts/check_install.py` reports, for the chosen runner, whether `claude` is on PATH or
whether an API credential resolves.

## Consequences

- Public links from ADRs to `docs/specs/` dangle in the mirror. That is accepted: the specs
  stay private and the ADRs stand on their own.
- Every PR runs a job that needs the `KB_DEPLOY_KEY` secret. A missing or revoked key turns
  `kb-guard` red, which blocks the sync. That is the safe failure.
- Changes to app behaviour that the KB regression tests pin need a matching change in the KB
  repo. Those tests run in demat's CI whenever the KB checkout contains them.
- Fixing a leak means removing the content, not adding an allowlist entry. The allowlist is
  only for lines verified to carry no KB content.
