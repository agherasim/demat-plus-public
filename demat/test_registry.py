"""Test-definition loader + DB sync + lookup API. See ADR 0005, spec
`demat/test_registry.py`.

Pure functions plus one DB writer (`sync_to_db`). Never imports Streamlit.
Loading a definition must never touch the external knowledge base — an
absent or renamed KB is a normal state (ADR 0005 §1, §9.6).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tomllib
import unicodedata
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from demat import config


class MetricDef(BaseModel):
    key: str
    label: str
    value_type: str  # "int" | "real" | "text"
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    max_possible: float | None = None
    required: bool = False
    norm_scale: str | None = None
    norm_field: str | None = None
    position: int = 0  # assigned by the loader from file order


class SubtestDef(BaseModel):
    key: str
    printed_label: str | None = None
    name: str
    rw_max: float | None = None
    value_type: str = "real"  # "int" | "real"
    norm_scale: str | None = None
    position: int = 0  # assigned by the loader from file order


LEGACY_SUM_OVER = "subtests.rw"


class SumRule(BaseModel):
    """One `[[rules.sum]]` rule (ADR 0005, generalized by ADR 0015).

    Exactly one target: an overall metric (`metric`) or a subtest
    (`subtest`). `over` is either the legacy `"subtests.rw"` ("every
    subtest row"; metric target only — DEMAT 1+/2+/5+) or an explicit list
    of subtest keys (the grouped form, DEMAT 3+).
    """

    metric: str | None = None
    subtest: str | None = None
    over: str | list[str]

    @model_validator(mode="after")
    def _check_shape(self) -> SumRule:
        if (self.metric is None) == (self.subtest is None):
            raise ValueError(
                "a sum rule needs exactly one target: `metric` or `subtest`"
            )
        target = self.metric if self.metric is not None else self.subtest
        if isinstance(self.over, str):
            if self.over != LEGACY_SUM_OVER:
                raise ValueError(
                    f"sum rule for {target!r}: a string `over` must be "
                    f"{LEGACY_SUM_OVER!r}, got {self.over!r}"
                )
            if self.metric is None:
                raise ValueError(
                    f"sum rule for {target!r}: `over = {LEGACY_SUM_OVER!r}` "
                    "requires a `metric` target"
                )
        else:
            if not self.over:
                raise ValueError(f"sum rule for {target!r}: `over` is empty")
            if len(self.over) != len(set(self.over)):
                raise ValueError(
                    f"sum rule for {target!r}: duplicate key(s) in `over` {self.over}"
                )
            if self.subtest is not None and self.subtest in self.over:
                raise ValueError(
                    f"sum rule for {target!r}: `over` contains its own target"
                )
        return self

    @property
    def is_legacy(self) -> bool:
        return isinstance(self.over, str)

    @property
    def target_ref(self) -> str:
        """`subtest:<key>` / `metric:<key>` — same namespace as findings."""
        if self.subtest is not None:
            return f"subtest:{self.subtest}"
        return f"metric:{self.metric}"


class TestDefinition(BaseModel):
    __test__ = False  # not a pytest test class despite the name

    test_key: str
    test_version: str
    display_name: str
    extraction_skill: str
    aliases: list[str] = Field(default_factory=list)
    norm_test: str | None = None
    overall_metrics: list[MetricDef] = Field(default_factory=list)
    subtests: list[SubtestDef] = Field(default_factory=list)
    rules_sum: list[SumRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _assign_positions_and_check_uniqueness(self) -> TestDefinition:
        for i, m in enumerate(self.overall_metrics, start=1):
            m.position = i
        for i, s in enumerate(self.subtests, start=1):
            s.position = i

        metric_keys = [m.key for m in self.overall_metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError(
                f"{self.test_key}: duplicate overall_metrics key(s) in {metric_keys}"
            )
        subtest_keys = [s.key for s in self.subtests]
        if len(subtest_keys) != len(set(subtest_keys)):
            raise ValueError(
                f"{self.test_key}: duplicate subtests key(s) in {subtest_keys}"
            )
        self._check_sum_rules()
        return self

    def _check_sum_rules(self) -> None:
        """ADR 0015 load-time checks for `[[rules.sum]]`. Each raises a
        `ValueError` naming the definition."""
        name = self.test_key
        subtest_by_key = {s.key: s for s in self.subtests}
        metric_by_key = {m.key: m for m in self.overall_metrics}

        targets = [r.target_ref for r in self.rules_sum]
        dup_targets = sorted({t for t in targets if targets.count(t) > 1})
        if dup_targets:
            raise ValueError(f"{name}: more than one sum rule for {dup_targets}")

        subtest_rules = self.sum_rules_subtest()
        subtest_targets = {r.subtest for r in subtest_rules}
        if subtest_rules and any(r.is_legacy for r in self.rules_sum):
            raise ValueError(
                f"{name}: a definition with subtest-target sum rules must not "
                f"also use `over = {LEGACY_SUM_OVER!r}` (it would count every "
                "point twice)"
            )

        owner: dict[str, str] = {}
        for rule in self.rules_sum:
            if rule.metric is not None and rule.metric not in metric_by_key:
                raise ValueError(
                    f"{name}: sum rule target metric {rule.metric!r} is not a "
                    "declared overall metric"
                )
            if rule.subtest is not None and rule.subtest not in subtest_by_key:
                raise ValueError(
                    f"{name}: sum rule target subtest {rule.subtest!r} is not a "
                    "declared subtest"
                )
            if rule.is_legacy:
                continue
            assert isinstance(rule.over, list)
            unknown = [k for k in rule.over if k not in subtest_by_key]
            if unknown:
                raise ValueError(
                    f"{name}: sum rule for {rule.target_ref} names undeclared "
                    f"subtest key(s) {unknown}"
                )
            if rule.subtest is not None:
                chained = [k for k in rule.over if k in subtest_targets]
                if chained:
                    raise ValueError(
                        f"{name}: sum rule for {rule.target_ref} chains over "
                        f"other sum target(s) {chained} (not supported)"
                    )
                for k in rule.over:
                    if k in owner:
                        raise ValueError(
                            f"{name}: subtest {k!r} is a component of both "
                            f"{owner[k]} and {rule.subtest!r}"
                        )
                    owner[k] = rule.subtest

            component_maxima = [subtest_by_key[k].rw_max for k in rule.over]
            if any(m is None for m in component_maxima):
                continue
            component_sum = sum(m for m in component_maxima if m is not None)
            if rule.subtest is not None:
                target_max = subtest_by_key[rule.subtest].rw_max
            else:
                assert rule.metric is not None
                target_max = metric_by_key[rule.metric].max_possible
            if target_max is not None and abs(component_sum - target_max) > 1e-9:
                raise ValueError(
                    f"{name}: sum rule for {rule.target_ref}: components' "
                    f"maxima sum to {component_sum}, but the target's maximum "
                    f"is {target_max}"
                )

    # --- sum-rule helpers (ADR 0015) ------------------------------------

    def sum_rules_subtest(self) -> list[SumRule]:
        """Subtest-target sum rules, in file order."""
        return [r for r in self.rules_sum if r.subtest is not None]

    def sum_rules_metric(self) -> list[SumRule]:
        """Metric-target sum rules (legacy and list form), in file order."""
        return [r for r in self.rules_sum if r.metric is not None]

    def over_keys(self, rule: SumRule) -> list[str]:
        """The subtest keys `rule` sums: every declared subtest for the
        legacy `"subtests.rw"` form, otherwise the rule's own list."""
        if isinstance(rule.over, str):
            return [s.key for s in self.subtests]
        return list(rule.over)

    def component_keys(self) -> set[str]:
        """Union of all subtest-target rules' `over` lists — the rows that
        only exist to be summed into a subtest (DEMAT 3+ Aufgabentyp rows).
        Derived from the rules, never declared a second time (ADR 0015)."""
        keys: set[str] = set()
        for rule in self.sum_rules_subtest():
            keys.update(self.over_keys(rule))
        return keys

    def subtest_sum_targets(self) -> set[str]:
        """Keys of subtests that are the target of a sum rule."""
        return {r.subtest for r in self.sum_rules_subtest() if r.subtest is not None}


def _parse_definition_file(path: Path) -> TestDefinition:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path.name}: invalid TOML: {exc}") from exc

    rules = raw.pop("rules", {})
    sum_rules = rules.get("sum", []) if isinstance(rules, dict) else []
    raw["rules_sum"] = sum_rules

    try:
        return TestDefinition.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError et al.
        raise ValueError(f"{path.name}: {exc}") from exc


def load_definitions(directory: Path | None = None) -> list[TestDefinition]:
    """Parse and validate every `*.toml` under `directory` (default
    `config.TEST_DEFINITIONS_DIR`). Raises `ValueError` naming the offending
    file on a bad one. Never reads the knowledge base.
    """
    directory = directory or config.TEST_DEFINITIONS_DIR
    definitions = []
    for path in sorted(directory.glob("*.toml")):
        definitions.append(_parse_definition_file(path))
    return definitions


def sync_to_db(
    conn: sqlite3.Connection, directory: Path | None = None
) -> list[TestDefinition]:
    """Load every definition file and UPSERT it into the catalog tables.

    Per `test_version`, inside the caller's transaction: `ON CONFLICT DO
    UPDATE` on `test_definitions`, then `DELETE` + re-`INSERT` the child
    metric/subtest rows. NOT `INSERT OR IGNORE` — editing a definition file
    must actually change the stored rows (ADR 0005 §1).
    """
    definitions = load_definitions(directory)
    directory = directory or config.TEST_DEFINITIONS_DIR

    for defn in definitions:
        path = directory / f"{defn.test_key}.toml"
        file_bytes = path.read_bytes()
        definition_sha256 = hashlib.sha256(file_bytes).hexdigest()
        spec_json = defn.model_dump_json()

        conn.execute(
            """
            INSERT INTO test_definitions (
                test_version, test_key, display_name, extraction_skill,
                aliases_json, norm_test, definition_file, definition_sha256,
                spec_json, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(test_version) DO UPDATE SET
                test_key = excluded.test_key,
                display_name = excluded.display_name,
                extraction_skill = excluded.extraction_skill,
                aliases_json = excluded.aliases_json,
                norm_test = excluded.norm_test,
                definition_file = excluded.definition_file,
                definition_sha256 = excluded.definition_sha256,
                spec_json = excluded.spec_json,
                is_active = 1,
                loaded_at = datetime('now')
            """,
            (
                defn.test_version,
                defn.test_key,
                defn.display_name,
                defn.extraction_skill,
                json.dumps(defn.aliases, ensure_ascii=False),
                defn.norm_test,
                str(path),
                definition_sha256,
                spec_json,
            ),
        )

        conn.execute(
            "DELETE FROM test_definition_metrics WHERE test_version = ?",
            (defn.test_version,),
        )
        for m in defn.overall_metrics:
            conn.execute(
                """
                INSERT INTO test_definition_metrics (
                    test_version, metric_key, label, value_type, unit,
                    min_value, max_value, max_possible, required,
                    norm_scale, norm_field, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    defn.test_version,
                    m.key,
                    m.label,
                    m.value_type,
                    m.unit,
                    m.min_value,
                    m.max_value,
                    m.max_possible,
                    1 if m.required else 0,
                    m.norm_scale,
                    m.norm_field,
                    m.position,
                ),
            )

        conn.execute(
            "DELETE FROM test_definition_subtests WHERE test_version = ?",
            (defn.test_version,),
        )
        for s in defn.subtests:
            conn.execute(
                """
                INSERT INTO test_definition_subtests (
                    test_version, subtest_key, printed_label, name, rw_max,
                    value_type, norm_scale, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    defn.test_version,
                    s.key,
                    s.printed_label,
                    s.name,
                    s.rw_max,
                    s.value_type,
                    s.norm_scale,
                    s.position,
                ),
            )

    return definitions


# --- Lookup API (reads from the DB catalog) ---------------------------------


def _row_to_definition(conn: sqlite3.Connection, row: sqlite3.Row) -> TestDefinition:
    metrics_rows = conn.execute(
        "SELECT * FROM test_definition_metrics WHERE test_version = ? ORDER BY position",
        (row["test_version"],),
    ).fetchall()
    subtest_rows = conn.execute(
        "SELECT * FROM test_definition_subtests WHERE test_version = ? ORDER BY position",
        (row["test_version"],),
    ).fetchall()
    # `rules.sum` has no catalog table of its own; it lives only in the
    # stored `spec_json` (the whole parsed definition, ADR 0005). Without
    # this, every DB-loaded definition silently had `rules_sum == []`.
    spec = json.loads(row["spec_json"] or "{}")

    return TestDefinition(
        test_key=row["test_key"],
        test_version=row["test_version"],
        display_name=row["display_name"],
        extraction_skill=row["extraction_skill"],
        aliases=json.loads(row["aliases_json"] or "[]"),
        norm_test=row["norm_test"],
        overall_metrics=[
            MetricDef(
                key=m["metric_key"],
                label=m["label"],
                value_type=m["value_type"],
                unit=m["unit"],
                min_value=m["min_value"],
                max_value=m["max_value"],
                max_possible=m["max_possible"],
                required=bool(m["required"]),
                norm_scale=m["norm_scale"],
                norm_field=m["norm_field"],
                position=m["position"],
            )
            for m in metrics_rows
        ],
        subtests=[
            SubtestDef(
                key=s["subtest_key"],
                printed_label=s["printed_label"],
                name=s["name"],
                rw_max=s["rw_max"],
                value_type=s["value_type"],
                norm_scale=s["norm_scale"],
                position=s["position"],
            )
            for s in subtest_rows
        ],
        rules_sum=[SumRule.model_validate(r) for r in spec.get("rules_sum", [])],
    )


def get(conn: sqlite3.Connection, test_version: str | None) -> TestDefinition | None:
    """Look up an active definition by its exact `test_version` label."""
    if not test_version:
        return None
    row = conn.execute(
        "SELECT * FROM test_definitions WHERE test_version = ? AND is_active = 1",
        (test_version,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_definition(conn, row)


def all_active(conn: sqlite3.Connection) -> list[TestDefinition]:
    rows = conn.execute(
        "SELECT * FROM test_definitions WHERE is_active = 1 ORDER BY test_key"
    ).fetchall()
    return [_row_to_definition(conn, row) for row in rows]


def resolve_alias(conn: sqlite3.Connection, raw_version: str | None) -> str | None:
    """Resolve a raw triage-emitted version string to a canonical
    `test_version`, via exact match or an alias. Returns None if unresolved.
    """
    if not raw_version:
        return None
    row = conn.execute(
        "SELECT test_version FROM test_definitions WHERE test_version = ? AND is_active = 1",
        (raw_version,),
    ).fetchone()
    if row is not None:
        return row["test_version"]

    rows = conn.execute(
        "SELECT test_version, aliases_json FROM test_definitions WHERE is_active = 1"
    ).fetchall()
    for r in rows:
        aliases = json.loads(r["aliases_json"] or "[]")
        if raw_version in aliases:
            return r["test_version"]
    return None


def skill_for(conn: sqlite3.Connection, test_version: str | None) -> str | None:
    defn = get(conn, test_version)
    return defn.extraction_skill if defn else None


def _strip_diacritics(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def canonical_subtest_key(defn: TestDefinition, emitted: str) -> str | None:
    """Resolve an emitted subtest key/label to the definition's canonical
    `key`, per ADR 0005 §9.2d's four-step order:

    1. exact `key` match
    2. exact `printed_label` match
    3. case-insensitive `key` match
    4. diacritic-stripped match (NFKD, drop combining marks)

    Deliberately does NOT use `identity.normalize_name` — its German folding
    (ä -> ae) would map "LÄ" -> "lae", which matches nothing; diacritic
    stripping maps "LÄ" -> "LA".

    Returns None if no subtest in `defn` matches by any of the four rules.
    """
    if not emitted:
        return None

    for s in defn.subtests:
        if s.key == emitted:
            return s.key

    for s in defn.subtests:
        if s.printed_label == emitted:
            return s.key

    emitted_lower = emitted.lower()
    for s in defn.subtests:
        if s.key.lower() == emitted_lower:
            return s.key

    emitted_stripped = _strip_diacritics(emitted).lower()
    for s in defn.subtests:
        if _strip_diacritics(s.key).lower() == emitted_stripped:
            return s.key
        if (
            s.printed_label
            and _strip_diacritics(s.printed_label).lower() == emitted_stripped
        ):
            return s.key

    return None


def prompt_contract(defn: TestDefinition | None) -> str:
    """Render the definition's expected metric/subtest keys and labels as
    text to inject into the extraction prompt at call time (ADR 0005 §4).

    Returns only the body (no heading) — the caller (`agent_runner`) supplies
    a neutral heading, distinct from the retry-only `extra_instructions`
    wording.
    """
    if defn is None:
        return ""

    lines = [f"Test: {defn.test_version} ({defn.display_name})"]
    if defn.overall_metrics:
        lines.append(
            "Overall metrics (emit each as {key, value, value_state, confidence}; "
            "value_state is written/blank/illegible):"
        )
        for m in defn.overall_metrics:
            req = " (required)" if m.required else ""
            lines.append(f"  - {m.key}: {m.label}{req}")
    if defn.subtests:
        lines.append(
            "Subtests (emit each with `key` set to the canonical key below; "
            "each also carries `rw_state`, `pr` and `pr_state`, "
            "written/blank/illegible):"
        )
        for s in defn.subtests:
            label = s.printed_label or s.key
            lines.append(
                f"  - key={s.key} printed_label={label}: {s.name} (rw_max={s.rw_max})"
            )
    subtest_rules = defn.sum_rules_subtest()
    if subtest_rules:
        lines.append(
            "Sum groups (the sheet's Rohwert-Summe cells). Transcribe a sum cell "
            "ONLY if a value is handwritten in it; set rw_state to "
            "written/blank/illegible. NEVER add the rows up yourself — the app "
            "computes blank sums:"
        )
        list_metric_rules = [r for r in defn.sum_rules_metric() if not r.is_legacy]
        for rule in [*subtest_rules, *list_metric_rules]:
            target = rule.subtest if rule.subtest is not None else rule.metric
            lines.append(f"  - {target} = {' + '.join(defn.over_keys(rule))}")
    return "\n".join(lines)
