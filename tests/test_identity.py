"""Unit tests for `demat.identity` (ADR 0001, amended by ADR 0006: the
normalization key is name-only — klasse is no longer a component).
"""

from __future__ import annotations

import pytest

from demat import identity


def test_normalize_name_german_folding_order():
    assert identity.normalize_name("Müller") == "mueller"
    assert identity.normalize_name("Straße") == "strasse"
    assert identity.normalize_name("Björn Öztürk") == "bjoern oeztuerk"


def test_normalize_name_empty_and_none():
    assert identity.normalize_name(None) == ""
    assert identity.normalize_name("") == ""
    assert identity.normalize_name("   ") == ""


def test_normalize_name_punctuation_only():
    assert identity.normalize_name("...---!!!") == ""


def test_normalize_name_collapses_whitespace_and_non_alnum():
    assert identity.normalize_name("  Max   Mustermann.  ") == "max mustermann"


def test_normalization_key_is_name_only_no_klasse_parameter():
    # ADR 0006: normalization_key(name) takes exactly one argument now.
    key = identity.normalization_key("Max Mustermann")
    assert key == "max mustermann"


def test_normalization_key_same_name_different_klasse_is_identical():
    # The whole point of ADR 0006: klasse must not affect the key, so a
    # child retested a year later in a new class still resolves to the same
    # normalization_key (and hence the same person_id).
    key_year_1 = identity.normalization_key("Leon Müller")
    key_year_2 = identity.normalization_key("Leon Müller")
    assert key_year_1 == key_year_2 == "leon mueller"


def test_person_id_deterministic_for_same_key_and_salt():
    salt = "test-salt"
    id_1 = identity.person_id("leon mueller", salt)
    id_2 = identity.person_id("leon mueller", salt)
    assert id_1 == id_2
    assert len(id_1) == 4


def test_person_id_differs_across_salts():
    key = "leon mueller"
    id_a = identity.person_id(key, "salt-a")
    id_b = identity.person_id(key, "salt-b")
    assert id_a != id_b


def test_person_id_for_returns_id_and_key():
    salt = "test-salt"
    person_id, norm_key = identity.person_id_for("Leon Müller", salt)
    assert norm_key == "leon mueller"
    assert person_id == identity.person_id(norm_key, salt)


def test_person_id_excludes_confusable_characters():
    # ADR 0001 addendum: '0' and 'O' are dropped from the alphabet because
    # these IDs are meant to be copied onto paper by hand.
    salt = "test-salt"
    for i in range(500):
        pid = identity.person_id(f"child {i}", salt)
        assert "0" not in pid
        assert "O" not in pid
        assert len(pid) == 4


def test_person_id_for_same_name_across_two_klasse_values_is_stable():
    # Simulates the cross-year-history scenario ADR 0006 exists for: the
    # same child, same name, different klasse recorded elsewhere on the
    # assessment (klasse is not passed to identity at all anymore).
    salt = "test-salt"
    id_class_2b, key_2b = identity.person_id_for("Leon Müller", salt)
    id_class_3b, key_3b = identity.person_id_for("Leon Müller", salt)
    assert id_class_2b == id_class_3b
    assert key_2b == key_3b


def test_disambiguated_id_and_key_substitutes_last_char_keeping_4_chars():
    base_id, base_key = identity.person_id_for("Sina Müller", "test-salt")
    new_id, new_key = identity.disambiguated_id_and_key(base_id, base_key)

    assert len(new_id) == 4
    assert new_id[:3] == base_id[:3]
    assert new_id[-1] != base_id[-1]
    assert new_id[-1] not in ("0", "O")
    assert new_id[-1] in identity._ID_ALPHABET
    assert new_key == f"{base_key}#{new_id[-1]}"


def test_disambiguated_id_and_key_avoids_excluded_ids(monkeypatch):
    # First candidate is excluded, forcing a retry; the retry loop must
    # produce the next candidate instead of giving up.
    candidates = iter(["B", "D"])
    monkeypatch.setattr(identity.secrets, "choice", lambda seq: next(candidates))

    new_id, new_key = identity.disambiguated_id_and_key(
        "DMZC", "sina mueller", exclude={"DMZB"}
    )

    assert new_id == "DMZD"
    assert new_key == "sina mueller#D"


def test_disambiguated_id_and_key_raises_once_alternates_are_exhausted():
    alphabet = identity._ID_ALPHABET
    exclude = {"DMZ" + c for c in alphabet if c != "C"}

    with pytest.raises(ValueError):
        identity.disambiguated_id_and_key("DMZC", "sina mueller", exclude=exclude)


def test_disambiguated_id_and_key_is_random_across_calls():
    base_id, base_key = identity.person_id_for("Sina Müller", "test-salt")
    results = {
        identity.disambiguated_id_and_key(base_id, base_key)[0] for _ in range(50)
    }
    # Not a strict guarantee, but with 33 possible substitute characters,
    # 50 draws should not all collapse onto one value.
    assert len(results) > 1
