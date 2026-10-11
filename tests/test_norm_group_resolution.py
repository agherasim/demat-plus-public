"""ADR 0010: `resolve_norm_group(norm_test, norm_variant)` for every
`(norm_test, variant)` pair the knowledge base publishes, correct German
labels keyed by `(norm_test, variant)` (not group name alone — the DEMAT 1+
regression this fixes), and the grade cross-check warning against the
chosen variant (never the test date).
"""

from __future__ import annotations

import pytest

from demat.reports import norms

# --- resolve_norm_group() -----------------------------------------------


@pytest.mark.parametrize(
    "norm_test,variant,expected_group,expected_label",
    [
        ("demat1plus", "k1", "k1_gesamt", "1. Klasse, letzter Schuljahresmonat"),
        (
            "demat1plus",
            "k2",
            "k2_gesamt",
            "2. Klasse, erste drei Schuljahresmonate",
        ),
        ("demat2plus", "k2", "k2_gesamt", "2. Klasse, letzter Schuljahresmonat"),
        (
            "demat2plus",
            "k3",
            "k3_gesamt",
            "3. Klasse, erste drei Schuljahresmonate",
        ),
        ("demat3plus", "ende_k3", "ende_k3", "Ende Klassenstufe 3"),
        ("demat3plus", "anfang_k4", "anfang_k4", "Anfang Klassenstufe 4"),
        ("demat5plus", "übergreifend", "übergreifend", "Schulformübergreifend"),
        ("demat5plus", "gymnasium", "gymnasium", "Gymnasium"),
        ("demat5plus", "realschule", "realschule", "Realschule"),
        ("demat5plus", "hauptschule", "hauptschule", "Hauptschule"),
        ("demat5plus", "sekundarschule", "sekundarschule", "Sekundarschule"),
    ],
)
def test_resolve_norm_group_table(norm_test, variant, expected_group, expected_label):
    group, label, returned_variant = norms.resolve_norm_group(norm_test, variant)
    assert group == expected_group
    assert label == expected_label
    assert returned_variant == variant
    # _gesamt, never _mädchen/_jungen (ADR 0010).
    assert "mädchen" not in group
    assert "jungen" not in group


def test_resolve_norm_group_demat1plus_k2_is_not_labelled_ende_2_klasse():
    """Regression: DEMAT 2+'s `k2_gesamt` is "Ende 2. Klasse", but DEMAT
    1+'s `k2` is the *start* of grade 2 — labels must be keyed by
    (norm_test, variant), not by group name alone."""
    _group, label, _variant = norms.resolve_norm_group("demat1plus", "k2")
    assert label == "2. Klasse, erste drei Schuljahresmonate"
    assert "Ende" not in label


def test_resolve_norm_group_raises_on_none_variant():
    with pytest.raises(ValueError):
        norms.resolve_norm_group("demat2plus", None)


def test_resolve_norm_group_raises_on_variant_valid_elsewhere_not_here():
    # 'gymnasium' is valid for demat5plus, not demat3plus.
    with pytest.raises(ValueError):
        norms.resolve_norm_group("demat3plus", "gymnasium")


# --- grade_cross_check_warning() -----------------------------------------


def test_grade_cross_check_agrees_no_warning():
    warning = norms.grade_cross_check_warning("3C", "demat2plus", "k3", "k3_gesamt")
    assert warning is None


def test_grade_cross_check_disagrees_fires_warning():
    warning = norms.grade_cross_check_warning(
        "3C", "demat3plus", "anfang_k4", "anfang_k4"
    )
    assert warning is not None
    assert "3C" in warning
    assert "anfang_k4" in warning


def test_grade_cross_check_skipped_for_demat5plus_school_types():
    assert (
        norms.grade_cross_check_warning("5A", "demat5plus", "gymnasium", "gymnasium")
        is None
    )


def test_grade_cross_check_skipped_without_leading_digit():
    assert (
        norms.grade_cross_check_warning("AB", "demat2plus", "k3", "k3_gesamt") is None
    )


def test_grade_cross_check_skipped_when_klasse_is_none():
    assert (
        norms.grade_cross_check_warning(None, "demat2plus", "k3", "k3_gesamt") is None
    )
