#!/usr/bin/env python3
"""Installer smoke test (ADR 0009 §6), replacing the `demat --check`
subcommand that was ruled out (no `[project.scripts]`; the project stays a
uv virtual project).

Imports `demat.config`, creates the data home, and prints what it resolved:
`CODE_ROOT`, `DEMAT_HOME`, `DATA_DIR`, the selected runner, whether the
knowledge base directory is present and non-empty, whether that runner can
reach Claude (an Anthropic credential for `direct`, the `claude` CLI on PATH
for `agent_sdk`), and whether every test definition loads. Never
prints key material — only booleans.

An absent knowledge base or credential is a normal, expected state on a
fresh/non-interactive install (both are provisioned by the user after the
fact) and does not fail the check; only a broken test-definition load
(a genuine installation defect) does.

Invoked by `demat setup` (`packaging/demat`, ADR 0017) as:

    uv run --frozen --no-dev --directory <code root> python -m scripts.check_install
"""

from __future__ import annotations

import shutil
import sys

from demat import config, test_registry


def _kb_present() -> bool:
    kb = config.KNOWLEDGE_BASE_DIR
    return kb.is_dir() and any(kb.iterdir())


def _anthropic_credential_resolves() -> bool:
    try:
        import anthropic

        anthropic.Anthropic()
    except Exception:
        return False
    return True


def _test_definitions_load() -> bool:
    try:
        definitions = test_registry.load_definitions()
    except Exception as exc:
        print(f"  error: {exc}")
        return False
    return len(definitions) > 0


def main() -> int:
    config.ensure_data_dirs()

    ok = True

    print(f"CODE_ROOT:  {config.CODE_ROOT}")
    print(f"DEMAT_HOME: {config.DEMAT_HOME}")
    print(f"DATA_DIR:   {config.DATA_DIR}")
    print(f"runner:     {config.RUNNER}")

    kb_present = _kb_present()
    print(f"knowledge base present and non-empty: {kb_present}")
    if not kb_present:
        print(f"  (expected on a fresh install; see {config.KNOWLEDGE_BASE_DIR})")

    if config.RUNNER == "agent_sdk":
        cli_ok = shutil.which("claude") is not None
        print(f"Claude Code CLI found: {cli_ok}")
        if not cli_ok:
            print("  (install it and run `claude` once to log in)")
    else:
        credential_ok = _anthropic_credential_resolves()
        print(f"Anthropic credential resolves: {credential_ok}")
        if not credential_ok:
            print("  (expected until ANTHROPIC_API_KEY is set in config.env)")

    definitions_ok = _test_definitions_load()
    print(f"test definitions load: {definitions_ok}")
    if not definitions_ok:
        ok = False

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
