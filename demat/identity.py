"""Deterministic pseudonymous 4-character child ID. Pure, no I/O. See ADR 0001
(amended by ADR 0006: the key is name-only, klasse no longer a component).

This module's German folding (`ä -> ae` etc) is for PERSON NAMES ONLY. Never
apply `normalize_name` to a knowledge-base identifier (Normgruppe strings
like `k3_mädchen`, or subtest scale/label matching) — see ADR 0005 §9.2b-c.
KB-facing matching lives in `test_registry.canonical_subtest_key`, which uses
diacritic stripping instead.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata

# Base36 minus '0'/'O' (ADR 0001 addendum): those two are the pair most
# often misread or miswritten by hand, and this ID is meant to be copied
# onto paper. Not a literal base-36 encoding anymore, but the encoding
# scheme (positional, divmod-based) is unchanged — just a smaller alphabet.
_ID_ALPHABET = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
_ID_SPACE = len(_ID_ALPHABET) ** 4

_GERMAN_FOLDS = (
    ("ß", "ss"),
    ("Ä", "Ae"),
    ("Ö", "Oe"),
    ("Ü", "Ue"),
    ("ä", "ae"),
    ("ö", "oe"),
    ("ü", "ue"),
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(s: str | None) -> str:
    """Normalize a name (or class) string per ADR 0001, order load-bearing."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for src, dst in _GERMAN_FOLDS:
        s = s.replace(src, dst)
    s = s.lower()
    s = _NON_ALNUM_RE.sub(" ", s)
    return " ".join(s.split())


def normalization_key(name: str | None) -> str:
    return normalize_name(name)


def _encode(n: int) -> str:
    base = len(_ID_ALPHABET)
    if n == 0:
        return _ID_ALPHABET[0]
    digits = []
    while n:
        n, rem = divmod(n, base)
        digits.append(_ID_ALPHABET[rem])
    return "".join(reversed(digits))


def person_id(key: str, salt: str) -> str:
    digest = hashlib.blake2b(
        key.encode("utf-8"), key=salt.encode("utf-8"), digest_size=8
    ).digest()
    n = int.from_bytes(digest, "big") % _ID_SPACE
    return _encode(n).rjust(4, _ID_ALPHABET[0])


def person_id_for(name: str | None, salt: str) -> tuple[str, str]:
    """Convenience: returns (person_id, normalization_key)."""
    key = normalization_key(name)
    return person_id(key, salt), key


# For each alphabet character, every other character in the alphabet — the
# substitution pool `disambiguated_id_and_key` draws from. Built once at
# import time; pure lookup, no randomness here.
_SUBSTITUTES: dict[str, tuple[str, ...]] = {
    ch: tuple(c for c in _ID_ALPHABET if c != ch) for ch in _ID_ALPHABET
}


def disambiguated_id_and_key(
    base_person_id: str, base_key: str, exclude: set[str] | None = None
) -> tuple[str, str]:
    """The same-name-collision escape hatch: two genuinely different children
    who normalize to the same `base_key` (and hence the same 4-character
    `base_person_id`) cannot both get a `persons` row — `normalization_key`
    stays UNIQUE, deliberately untouched. Instead of merging them, the
    second (and any later) occurrence keeps `person_id` at exactly 4
    characters (ADR 0001's length requirement is never broken): its last
    character is replaced with a different one drawn from `_SUBSTITUTES`,
    still recognisably close to the original. `base_key` gets that
    replacement character appended as a marker instead — that column has no
    length constraint — which is what actually satisfies `UNIQUE` without
    changing it.

    Known limitation, left standing on purpose: a later, separately-
    processed sheet for the *moved* child will still recompute the plain
    `base_person_id`/`base_key` and re-merge into the original person —
    resolving that fully needs the person-relink mechanism ADR 0006 defers
    until there's a demonstrated need for it.
    """
    exclude = exclude or set()
    prefix, original_char = base_person_id[:-1], base_person_id[-1]
    remaining = list(_SUBSTITUTES[original_char])
    while remaining:
        substitute = secrets.choice(remaining)
        remaining.remove(substitute)
        candidate_id = prefix + substitute
        if candidate_id not in exclude:
            return candidate_id, f"{base_key}#{substitute}"
    raise ValueError(
        f"exhausted substitution alternates for {base_person_id!r} in this batch"
    )
