"""ADR 0008 spec §6.2.3/§6.2.4/§10: `class_norm` gated on
`norm_test == "demat3plus"`, `--scores` fed Rohwerte not PRs, the
KB-absent path, and `unsicher`/`leer_im_original`/`druckfehler_vermutet`
status propagation.
"""

from __future__ import annotations

import pytest

from demat import config
from demat.reports import norms

requires_kb = pytest.mark.skipif(
    not config.KNOWLEDGE_BASE_DIR.exists(),
    reason=f"knowledge base absent: {config.KNOWLEDGE_BASE_DIR}",
)


# --- class_norm() is DEMAT 3+ only, structurally -----------------------------


@requires_kb
def test_class_norm_needs_at_least_two_scores():
    assert norms.class_norm("GES", [12.0]) is None


@requires_kb
def test_class_norm_with_rohwerte_returns_deciles_and_interpretation():
    result = norms.class_norm("GES", [12, 18, 25, 20, 15, 22, 10, 28])
    assert result is not None
    assert result["test"] == "demat3plus"
    assert "klassenmittelwert" in result
    assert "klassenstreuung" in result
    assert "mean_decile" in result
    assert "spread_decile" in result


@requires_kb
def test_class_norm_unknown_scale_fails():
    with pytest.raises(RuntimeError):
        norms.class_norm("NOT_A_SCALE", [1, 2, 3])


@requires_kb
def test_class_norm_scale_vocabulary_matches_demat3plus_subtest_keys():
    """The CLI's `class` subparser's `--scale` vocabulary
    (`ZS AD SU MU SR SZ FL LS LU` / `ARI SAR GEO GES`) must include every
    subtest key `demat3plus.toml` declares, plus the total `GES` — this is
    the "easiest consequential mistake" the spec calls out.

    The sheet prints Sachrechnungen as two cells, SR1 and SR2 (ADR 0015),
    while the KB's class norm has a single `SR` scale for SR1+SR2 — so both
    map to `SR` before the subset check."""
    from demat.test_registry import load_definitions

    defn = next(d for d in load_definitions() if d.test_key == "demat3plus")
    class_scale = {"SR1": "SR", "SR2": "SR"}
    subtest_keys = {class_scale.get(s.key, s.key) for s in defn.subtests}
    assert subtest_keys <= {
        "ZS",
        "AD",
        "SU",
        "MU",
        "SR",
        "SZ",
        "FL",
        "LS",
        "LU",
        "ARI",
        "SAR",
        "GEO",
    }

    # Each of the definition's own subtest keys must actually work as a scale.
    for key in subtest_keys:
        result = norms.class_norm(key, [1, 2, 3])
        assert result is not None
        assert result["skala"] == key


# --- KB status propagation via norm() ----------------------------------------


@requires_kb
def test_norm_ok_status_returns_pr_and_classification():
    result = norms.norm("demat2plus", "gesamtwert", "k3_gesamt", 30)
    assert result["ok"] is True
    assert result["status"] in ("ok", "unsicher", "leer_im_original")
    assert "classification" in result


@requires_kb
def test_norm_raises_on_bad_scale_group():
    with pytest.raises(RuntimeError):
        norms.norm("demat2plus", "gesamtwert", "not_a_group", 30)


@requires_kb
def test_norm_propagates_status_and_warnings_when_present():
    """Scan the whole normen.csv for at least one row of each interesting
    status and confirm `norm()` surfaces the documented behaviour: `ok`
    passes through, `unsicher` is used and annotated, `leer_im_original`
    means pr is null with a caveat, `druckfehler_vermutet` is refused
    unless `allow_flagged=True`.
    """
    import csv

    normen_csv = config.KNOWLEDGE_BASE_DIR / "data" / "demat2plus" / "normen.csv"
    if not normen_csv.exists():
        pytest.skip("demat2plus/normen.csv not present")

    with normen_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_status: dict[str, dict] = {}
    for row in rows:
        by_status.setdefault(row["status"], row)

    if "unsicher" in by_status:
        row = by_status["unsicher"]
        result = norms.norm(
            "demat2plus", row["skala"], row["normgruppe"], row["rohwert"]
        )
        assert result["status"] == "unsicher"
        assert result["warnings"]

    if "leer_im_original" in by_status:
        row = by_status["leer_im_original"]
        result = norms.norm(
            "demat2plus", row["skala"], row["normgruppe"], row["rohwert"]
        )
        assert result["status"] == "leer_im_original"
        assert result["pr"] is None

    if "druckfehler_vermutet" in by_status:
        row = by_status["druckfehler_vermutet"]
        with pytest.raises(RuntimeError):
            norms.norm("demat2plus", row["skala"], row["normgruppe"], row["rohwert"])
        # Allowed through explicitly, with a warning.
        result = norms.norm(
            "demat2plus",
            row["skala"],
            row["normgruppe"],
            row["rohwert"],
            allow_flagged=True,
        )
        assert result["status"] == "druckfehler_vermutet"
        assert result["warnings"]


# --- KB-absent path -----------------------------------------------------------


def test_kb_unavailable_raises_documented_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KNOWLEDGE_BASE_DIR", tmp_path / "no-such-kb")
    assert norms.kb_available() is False
    with pytest.raises(norms.KnowledgeBaseUnavailableError):
        norms.classify("demat2plus", pr=50)
