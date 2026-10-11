#!/usr/bin/env python3
"""Copy `demat/skills/_source/reference.md` into every skill directory, and
regenerate the triage skill's known-version list from the test-definition
registry.

Run after editing `_source/reference.md` or any `demat/test_definitions/*.toml`.
`tests/test_skills_sync.py` asserts both are current so neither silently drifts.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "demat" / "skills"
SOURCE_REFERENCE = SKILLS_DIR / "_source" / "reference.md"
TRIAGE_SKILL_MD = SKILLS_DIR / "demat-sheet-triage" / "SKILL.md"

_KNOWN_VERSIONS_RE = re.compile(
    r"(<!-- KNOWN_VERSIONS:START.*?-->\n)(.*?)(\n<!-- KNOWN_VERSIONS:END -->)",
    re.DOTALL,
)

sys.path.insert(0, str(REPO_ROOT))


def skill_dirs() -> list[Path]:
    return sorted(
        d
        for d in SKILLS_DIR.iterdir()
        if d.is_dir() and d.name != "_source" and (d / "SKILL.md").exists()
    )


def sync_reference() -> list[Path]:
    content = SOURCE_REFERENCE.read_text(encoding="utf-8")
    written = []
    for skill_dir in skill_dirs():
        target = skill_dir / "reference.md"
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def known_versions() -> list[str]:
    from demat import test_registry

    definitions = test_registry.load_definitions()
    return [d.test_version for d in definitions]


def sync_triage_known_versions() -> bool:
    """Regenerate the KNOWN_VERSIONS block in the triage skill's SKILL.md
    from the test-definition registry. Returns True if the file changed.
    """
    text = TRIAGE_SKILL_MD.read_text(encoding="utf-8")
    versions = known_versions()
    block = "\n".join(f"- {v}" for v in versions)
    new_text, n = _KNOWN_VERSIONS_RE.subn(rf"\g<1>{block}\g<3>", text)
    if n == 0:
        raise RuntimeError(
            f"{TRIAGE_SKILL_MD}: KNOWN_VERSIONS markers not found; cannot sync"
        )
    if new_text != text:
        TRIAGE_SKILL_MD.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    written = sync_reference()
    for path in written:
        print(f"synced {path.relative_to(REPO_ROOT)}")
    if sync_triage_known_versions():
        print(f"synced {TRIAGE_SKILL_MD.relative_to(REPO_ROOT)} (known versions)")
    else:
        print(
            f"{TRIAGE_SKILL_MD.relative_to(REPO_ROOT)} (known versions) already current"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
