"""ADR 0008 spec §6.4/§10: numeric containment (pass/fail), the diagnosis
regex including the referral carve-out, and the coverage check.
"""

from __future__ import annotations

from demat.reports.data import ChildData, ClassProfileData, MissingDataAudit
from demat.reports.prose import (
    ChildProse,
    ClassProse,
    validate_coverage,
    validate_diagnosis_terms,
    validate_numeric_containment,
)


def _data(children_refs: list[str]) -> ClassProfileData:
    children = [
        ChildData(
            assessment_id=i,
            person_id=ref,
            person_ref=ref,
            display_label=ref,
            overall_value=50.0,
            overall_category="durchschnittlich",
            subtest_pr={"ZE": 60},
            subtest_rw={"ZE": 2.0},
        )
        for i, ref in enumerate(children_refs, start=1)
    ]
    return ClassProfileData(
        klasse="3C",
        schuljahr="2026-27",
        test_version="DEMAT 2+",
        test_key="demat2plus",
        norm_test="demat2plus",
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        child_count=len(children),
        awaiting_review_count=0,
        classification_basis="pr",
        categories_ordered=["schwach", "durchschnittlich", "gut"],
        norm_group="k3_gesamt",
        norm_group_label="Anfang 3. Klasse",
        norm_variant="k3",
        cross_check_warning=None,
        children=children,
        distribution=[],
        class_mean=50,
        class_median=50,
        class_n=len(children),
        subtest_rows=[],
        missing_data=MissingDataAudit(
            children_without_overall=[], subtests_with_zero_present=[]
        ),
        has_class_norms=False,
    )


def _prose(refs: list[str], **overrides) -> ClassProse:
    defaults = {
        "profil_staerken": "Die Klasse zeigt solide Leistungen im Bereich Addition.",
        "profil_schwaechen": "Einzelne Kinder zeigen Schwächen im Bereich Geometrie.",
        "profil_muster": "Kein auffälliges Muster erkennbar.",
        "empfehlungen": ["Übung im Bereich Addition anbieten."],
        "kinder": [
            ChildProse(
                person_ref=ref,
                staerken="Stark im Kopfrechnen.",
                schwaechen="",
                empfehlungen=["Weiter üben."],
            )
            for ref in refs
        ],
    }
    defaults.update(overrides)
    return ClassProse(**defaults)


# --- numeric containment -----------------------------------------------------


def test_numeric_containment_passes_with_only_input_numbers():
    data = _data(["AAAA"])
    prose = _prose(
        ["AAAA"],
        profil_staerken="Der Prozentrang liegt bei 50 im Mittel.",
    )
    offending = validate_numeric_containment(prose, data.model_dump_json())
    assert offending == []


def test_numeric_containment_fails_on_an_invented_number():
    data = _data(["AAAA"])
    prose = _prose(
        ["AAAA"],
        profil_staerken="Die Klasse erreichte einen Mittelwert von 73.",
    )
    offending = validate_numeric_containment(prose, data.model_dump_json())
    assert "73" in offending


def test_numeric_containment_allows_small_whitelist_ordinals():
    data = _data(["AAAA"])
    prose = _prose(
        ["AAAA"],
        profil_staerken="Zwei der 100 möglichen Punkte fehlten nur knapp.",
    )
    offending = validate_numeric_containment(prose, data.model_dump_json())
    assert offending == []


def test_numeric_containment_handles_german_comma_decimals():
    data = _data(["AAAA"])
    # rw of 2.0 is in the input JSON as "2.0"; a German-comma-formatted
    # "2,0" in the prose must still normalise to the same allowed token.
    prose = _prose(["AAAA"], profil_staerken="Der Rohwert betrug 2,0 Punkte.")
    offending = validate_numeric_containment(prose, data.model_dump_json())
    assert offending == []


# --- diagnosis check with the referral carve-out -----------------------------


def test_diagnosis_bare_assertion_is_rejected():
    prose = _prose(
        ["AAAA"], profil_muster="Bei diesem Kind liegt eine Rechenschwäche vor."
    )
    offending = validate_diagnosis_terms(prose)
    assert offending


def test_diagnosis_referral_construction_is_allowed():
    prose = _prose(
        ["AAAA"],
        profil_muster="Bei auffälligen Ergebnissen sollte eine Förderdiagnostik "
        "(Rechenschwäche) erwogen werden.",
    )
    offending = validate_diagnosis_terms(prose)
    assert offending == []


def test_diagnosis_weitere_abklaerung_referral_is_allowed():
    prose = _prose(
        ["AAAA"],
        profil_muster="Eine weitere Abklärung einer möglichen Dyskalkulie kann "
        "sinnvoll sein.",
    )
    offending = validate_diagnosis_terms(prose)
    assert offending == []


def test_diagnosis_hat_eine_assertive_form_rejected_even_near_referral_word():
    prose = _prose(
        ["AAAA"],
        profil_muster="Das Kind hat eine Dyskalkulie, daher erwägen wir Maßnahmen.",
    )
    offending = validate_diagnosis_terms(prose)
    assert offending


# --- coverage check -----------------------------------------------------------


def test_coverage_passes_when_every_child_has_exactly_one_entry():
    data = _data(["AAAA", "BBBB"])
    prose = _prose(["AAAA", "BBBB"])
    assert validate_coverage(prose, data) == []


def test_coverage_fails_on_missing_child():
    data = _data(["AAAA", "BBBB"])
    prose = _prose(["AAAA"])
    problems = validate_coverage(prose, data)
    assert problems


def test_coverage_fails_on_unknown_person_ref():
    data = _data(["AAAA"])
    prose = _prose(["AAAA", "ZZZZ"])
    problems = validate_coverage(prose, data)
    assert problems


def test_coverage_fails_on_child_count_mismatch():
    data = _data(["AAAA", "BBBB", "CCCC"])
    prose = _prose(["AAAA", "BBBB"])
    problems = validate_coverage(prose, data)
    assert any("expected 3" in p for p in problems)
