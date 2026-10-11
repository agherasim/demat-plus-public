"""Tripwire for the next external-knowledge-base drop (ADR 0005 §9.1).

Skips entirely when `config.KNOWLEDGE_BASE_DIR`, or a specific test's
`data/<norm_test>/` subdirectory, is absent — the KB is untracked and a
fresh checkout legitimately has none. When present, asserts our
`test_definitions/*.toml` haven't drifted from it: every non-null
`norm_scale` must appear as a `skala` value in that test's `normen.csv`, and
every `rw_max` must match the numeric part of the `max=` entry in
`tabellen.json`'s `meta`, where one exists.
"""

from __future__ import annotations

import csv
import json
import re

import pytest

from demat import config, norm_variants, test_registry

_MAX_RE = re.compile(r"max=(\d+(?:[.,]\d+)?)")
_GENDER_SUFFIX_RE = re.compile(r"_(mädchen|jungen)$")


def _kb_test_dir(norm_test: str):
    return config.KNOWLEDGE_BASE_DIR / "data" / norm_test


def _require_kb_test(norm_test: str | None):
    if norm_test is None:
        pytest.skip("definition has no norm_test")
    if not config.KNOWLEDGE_BASE_DIR.exists():
        pytest.skip(f"knowledge base absent: {config.KNOWLEDGE_BASE_DIR}")
    test_dir = _kb_test_dir(norm_test)
    if not test_dir.exists():
        pytest.skip(f"knowledge base has no {norm_test}/ subdirectory")
    return test_dir


def _normen_skala_values(test_dir) -> set[str]:
    normen_csv = test_dir / "normen.csv"
    if not normen_csv.exists():
        pytest.skip(f"{normen_csv} not present")
    with normen_csv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["skala"] for row in reader}


def _tabellen_max_by_skala(test_dir) -> dict[str, float]:
    tabellen_json = test_dir / "tabellen.json"
    if not tabellen_json.exists():
        pytest.skip(f"{tabellen_json} not present")
    data = json.loads(tabellen_json.read_text(encoding="utf-8"))
    maxima: dict[str, float] = {}
    for entry in data.get("tabellen", []):
        skala = entry.get("skala")
        meta = entry.get("meta") or []
        for m in meta:
            match = _MAX_RE.search(m)
            if match and skala not in maxima:
                maxima[skala] = float(match.group(1).replace(",", "."))
    return maxima


@pytest.mark.parametrize(
    "test_key", ["demat1plus", "demat2plus", "demat3plus", "demat5plus"]
)
def test_norm_scale_join_fields_exist_in_normen_csv(test_key):
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    defn = definitions[test_key]
    test_dir = _require_kb_test(defn.norm_test)
    skala_values = _normen_skala_values(test_dir)

    checked_scales = [m.norm_scale for m in defn.overall_metrics if m.norm_scale] + [
        s.norm_scale for s in defn.subtests if s.norm_scale
    ]
    if not checked_scales:
        pytest.skip(f"{test_key}: no non-null norm_scale fields to check")

    for scale in checked_scales:
        assert scale in skala_values, (
            f"{test_key}: norm_scale {scale!r} not found in "
            f"{test_dir / 'normen.csv'}'s skala values {sorted(skala_values)}"
        )


@pytest.mark.parametrize(
    "test_key", ["demat1plus", "demat2plus", "demat3plus", "demat5plus"]
)
def test_rw_max_matches_tabellen_json_meta_where_present(test_key):
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    defn = definitions[test_key]
    test_dir = _require_kb_test(defn.norm_test)
    maxima = _tabellen_max_by_skala(test_dir)
    if not maxima:
        pytest.skip(f"{test_key}: tabellen.json has no 'max=' meta entries to check")

    checked = 0
    for s in defn.subtests:
        if s.norm_scale and s.norm_scale in maxima and s.rw_max is not None:
            checked += 1
            assert s.rw_max == maxima[s.norm_scale], (
                f"{test_key}: subtest {s.key!r} rw_max={s.rw_max} does not match "
                f"KB tabellen.json max={maxima[s.norm_scale]} for {s.norm_scale!r}"
            )
    for m in defn.overall_metrics:
        if m.norm_scale and m.norm_scale in maxima and m.max_possible is not None:
            checked += 1
            assert m.max_possible == maxima[m.norm_scale], (
                f"{test_key}: metric {m.key!r} max_possible={m.max_possible} does not "
                f"match KB tabellen.json max={maxima[m.norm_scale]} for {m.norm_scale!r}"
            )
    if checked == 0:
        pytest.skip(f"{test_key}: no rw_max/max_possible overlapped a KB 'max=' entry")


# --- demat/norm_variants.py drift (ADR 0010) --------------------------------
#
# `norm_variants.py` deliberately duplicates the KB's `normgruppe` values
# (KB-free by design, so the upload selector renders without a KB present —
# ADR 0005 §9.6). This is the only thing keeping that duplication honest; it
# skips without a KB, so a CI run without one will not catch drift.


@pytest.mark.parametrize(
    "norm_test", ["demat1plus", "demat2plus", "demat3plus", "demat5plus"]
)
def test_norm_variants_table_matches_kb_normgruppe_values(norm_test):
    test_dir = _require_kb_test(norm_test)

    normen_csv = test_dir / "normen.csv"
    if not normen_csv.exists():
        pytest.skip(f"{normen_csv} not present")
    with normen_csv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw_groups = {row["normgruppe"] for row in reader}

    gender_stripped = {
        _GENDER_SUFFIX_RE.sub("", g).removesuffix("_gesamt") for g in raw_groups
    }
    ours = {v.key for v in norm_variants.variants_for(norm_test)}
    assert ours == gender_stripped, (
        f"{norm_test}: demat/norm_variants.py variants {sorted(ours)} do not match "
        f"KB normgruppe values (gender-stripped) {sorted(gender_stripped)}"
    )

    has_gender_split = any(_GENDER_SUFFIX_RE.search(g) for g in raw_groups)
    assert norm_variants.GENDER_SPLIT.get(norm_test, False) == has_gender_split, (
        f"{norm_test}: GENDER_SPLIT={norm_variants.GENDER_SPLIT.get(norm_test)} "
        f"but KB gender-split presence is {has_gender_split}"
    )
