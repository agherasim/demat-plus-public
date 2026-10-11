"""Unit tests for `demat.test_registry` (ADR 0005 §1, §9.2d)."""

from __future__ import annotations

import sqlite3

import pytest

from demat import config, test_registry


def test_every_shipped_toml_parses_with_no_kb_present():
    # Definitions must load without touching the knowledge base at all
    # (ADR 0005 §1) — this test doesn't stub anything out; it just never
    # imports or references `config.KNOWLEDGE_BASE_DIR`.
    definitions = test_registry.load_definitions()
    assert len(definitions) >= 4
    keys = {d.test_key for d in definitions}
    assert keys == {"demat1plus", "demat2plus", "demat3plus", "demat5plus"}


def test_no_definition_references_demat4plus():
    definitions = test_registry.load_definitions()
    for d in definitions:
        assert d.test_key != "demat4plus"
        assert "4+" not in d.test_version
        assert "4plus" not in d.test_key


def test_extraction_skill_names_an_existing_skill_dir():
    definitions = test_registry.load_definitions()
    for d in definitions:
        skill_dir = config.SKILLS_DIR / d.extraction_skill
        assert skill_dir.is_dir(), f"{d.test_key}: no such skill dir {skill_dir}"
        assert (skill_dir / "SKILL.md").exists()


def test_keys_unique_within_a_definition():
    definitions = test_registry.load_definitions()
    for d in definitions:
        metric_keys = [m.key for m in d.overall_metrics]
        assert len(metric_keys) == len(set(metric_keys))
        subtest_keys = [s.key for s in d.subtests]
        assert len(subtest_keys) == len(set(subtest_keys))


def test_demat2plus_full_definition_shape():
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    demat2plus = definitions["demat2plus"]
    assert demat2plus.test_version == "DEMAT 2+"
    assert len(demat2plus.subtests) == 10
    assert len(demat2plus.overall_metrics) == 5

    rw_total = next(m for m in demat2plus.overall_metrics if m.key == "rw_total")
    assert rw_total.max_possible == 36
    assert rw_total.value_type == "real"

    sa = next(s for s in demat2plus.subtests if s.key == "SA")
    assert sa.value_type == "real"  # half-point steps, ADR 0005 §9.3

    la = next(s for s in demat2plus.subtests if s.key == "LA")
    assert la.printed_label == "LÄ"
    assert la.norm_scale == "subtest_LA"

    assert len(demat2plus.rules_sum) == 1
    assert demat2plus.rules_sum[0].metric == "rw_total"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    sql = config.SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    connection.executescript(sql)
    yield connection
    connection.close()


def test_sync_to_db_upserts_not_ignores(conn, tmp_path):
    definitions_dir = tmp_path / "defs"
    definitions_dir.mkdir()
    toml_path = definitions_dir / "demat2plus.toml"
    toml_path.write_text(
        """
test_key = "demat2plus"
test_version = "DEMAT 2+"
display_name = "Original display name"
extraction_skill = "demat-2plus-extraction"

[[overall_metrics]]
key = "rw_total"
label = "Rohwert gesamt"
value_type = "real"
max_possible = 36
""",
        encoding="utf-8",
    )

    test_registry.sync_to_db(conn, definitions_dir)
    conn.commit()
    row = conn.execute(
        "SELECT display_name FROM test_definitions WHERE test_version = 'DEMAT 2+'"
    ).fetchone()
    assert row["display_name"] == "Original display name"

    # THE anti-`INSERT OR IGNORE` regression test: editing the file and
    # re-running sync_to_db must change the stored row, not silently no-op.
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8").replace(
            "Original display name", "Edited display name"
        ),
        encoding="utf-8",
    )
    test_registry.sync_to_db(conn, definitions_dir)
    conn.commit()
    row = conn.execute(
        "SELECT display_name FROM test_definitions WHERE test_version = 'DEMAT 2+'"
    ).fetchone()
    assert row["display_name"] == "Edited display name"


def test_sync_to_db_replaces_child_rows_on_edit(conn, tmp_path):
    definitions_dir = tmp_path / "defs"
    definitions_dir.mkdir()
    toml_path = definitions_dir / "demat2plus.toml"
    toml_path.write_text(
        """
test_key = "demat2plus"
test_version = "DEMAT 2+"
display_name = "DEMAT 2+"
extraction_skill = "demat-2plus-extraction"

[[subtests]]
key = "ZE"
name = "Zahleneigenschaften"
rw_max = 2
value_type = "int"
""",
        encoding="utf-8",
    )
    test_registry.sync_to_db(conn, definitions_dir)
    conn.commit()
    rows = conn.execute(
        "SELECT subtest_key FROM test_definition_subtests WHERE test_version = 'DEMAT 2+'"
    ).fetchall()
    assert [r["subtest_key"] for r in rows] == ["ZE"]

    toml_path.write_text(
        """
test_key = "demat2plus"
test_version = "DEMAT 2+"
display_name = "DEMAT 2+"
extraction_skill = "demat-2plus-extraction"

[[subtests]]
key = "AD"
name = "Addition"
rw_max = 4
value_type = "int"
""",
        encoding="utf-8",
    )
    test_registry.sync_to_db(conn, definitions_dir)
    conn.commit()
    rows = conn.execute(
        "SELECT subtest_key FROM test_definition_subtests WHERE test_version = 'DEMAT 2+'"
    ).fetchall()
    assert [r["subtest_key"] for r in rows] == ["AD"]


def test_canonical_subtest_key_resolves_la_via_diacritic_stripping_not_folding():
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    demat2plus = definitions["demat2plus"]

    # Step 1: exact key match.
    assert test_registry.canonical_subtest_key(demat2plus, "LA") == "LA"
    # Step 2: exact printed_label match.
    assert test_registry.canonical_subtest_key(demat2plus, "LÄ") == "LA"
    # Step 3: case-insensitive key match.
    assert test_registry.canonical_subtest_key(demat2plus, "la") == "LA"
    # Step 4: diacritic-stripped match (NFKD, drop combining marks) —
    # NOT identity.normalize_name's German folding, which would yield "lae".
    assert test_registry.canonical_subtest_key(demat2plus, "lä") == "LA"

    assert test_registry.canonical_subtest_key(demat2plus, "XYZ") is None


def test_canonical_subtest_key_unrelated_definition_returns_none():
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    demat2plus = definitions["demat2plus"]
    assert test_registry.canonical_subtest_key(demat2plus, "ARI") is None


def test_prompt_contract_names_pr_state_and_value_state():
    # ADR 0013: without this, the Gesamtwert/subtest lookup is silently
    # never triggered, because the model is never told to report
    # pr_state/value_state in the first place.
    definitions = {d.test_key: d for d in test_registry.load_definitions()}
    contract = test_registry.prompt_contract(definitions["demat2plus"])
    assert "value_state" in contract
    assert "pr_state" in contract


def test_prompt_contract_none_definition_returns_empty_string():
    assert test_registry.prompt_contract(None) == ""
