"""The published norm-table variants per test (ADR 0010).

Mirrors the knowledge base's distinct `normgruppe` values with the
gender suffix stripped. Deliberately contains no KB access: the upload
selector must render when `config.KNOWLEDGE_BASE_DIR` is absent
(ADR 0005 §9.6). `tests/test_kb_consistency.py` asserts this table has
not drifted from the KB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NormVariant:
    key: str  # 'k2', 'ende_k3', 'gymnasium', ...
    label: str  # German label, from tabellen.json's `beschreibung`


VARIANTS: dict[str, list[NormVariant]] = {
    "demat1plus": [
        NormVariant("k1", "1. Klasse, letzter Schuljahresmonat"),
        NormVariant("k2", "2. Klasse, erste drei Schuljahresmonate"),
    ],
    "demat2plus": [
        NormVariant("k2", "2. Klasse, letzter Schuljahresmonat"),
        NormVariant("k3", "3. Klasse, erste drei Schuljahresmonate"),
    ],
    "demat3plus": [
        NormVariant("ende_k3", "Ende Klassenstufe 3"),
        NormVariant("anfang_k4", "Anfang Klassenstufe 4"),
    ],
    "demat5plus": [
        NormVariant("übergreifend", "Schulformübergreifend"),
        NormVariant("gymnasium", "Gymnasium"),
        NormVariant("realschule", "Realschule"),
        NormVariant("hauptschule", "Hauptschule"),
        NormVariant("sekundarschule", "Sekundarschule"),
    ],
}

GENDER_SPLIT: dict[str, bool] = {
    "demat1plus": True,
    "demat2plus": True,
    "demat3plus": False,
    "demat5plus": False,
}

_GRADE_RE = re.compile(r"^(?:ende_|anfang_)?k(\d+)$")


def variants_for(norm_test: str | None) -> list[NormVariant]:
    if norm_test is None:
        return []
    return VARIANTS.get(norm_test, [])


def is_valid(norm_test: str | None, variant: str | None) -> bool:
    if norm_test is None or variant is None:
        return False
    return any(v.key == variant for v in variants_for(norm_test))


def label_for(norm_test: str | None, variant: str | None) -> str | None:
    if norm_test is None or variant is None:
        return None
    for v in variants_for(norm_test):
        if v.key == variant:
            return v.label
    return None


def expected_grade(variant: str | None) -> int | None:
    if variant is None:
        return None
    match = _GRADE_RE.match(variant)
    if not match:
        return None
    return int(match.group(1))
