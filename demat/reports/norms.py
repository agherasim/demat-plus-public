"""Stage 3 — norms (ADR 0008 spec §6.2). Every function shells out to
`scripts/lookup.py` in the external knowledge base; none contains a
hardcoded band boundary or norm value. Never imports Streamlit.

The knowledge base is called from Python, never handed to the LLM (ADR
0008's central enforcement mechanism for KB rule 2) — this module is the
only caller of `lookup.py`.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from demat import config, norm_variants
from demat.test_registry import TestDefinition

ClassifyFn = Callable[[float | None], dict[str, Any]]

LOOKUP_SCRIPT = "scripts/lookup.py"

# The manual's published category set, worst -> best, per `norm_test`
# (spec §6.2.2's table, itself sourced from `lookup.py`'s BANDS — the
# category *labels and order* are structural metadata the manual publishes,
# not a numeric boundary; every actual band edge is still read from
# `classify()`/`lookup.py`, never duplicated here). Used only to render the
# distribution card row in manual order and to colour bars/sparklines by
# ordinal rank (spec §6.2.2, §6.5).
CATEGORIES_ORDERED: dict[str, list[str]] = {
    "demat1plus": [
        "weit unterdurchschnittlich",
        "unterdurchschnittlich",
        "durchschnittlich",
        "überdurchschnittlich",
        "weit überdurchschnittlich",
    ],
    "demat2plus": [
        "sehr schwach",
        "schwach",
        "unterdurchschnittlich",
        "durchschnittlich",
        "überdurchschnittlich",
        "ausgezeichnet",
    ],
    "demat3plus": [
        "sehr schwach",
        "schwach",
        "unterdurchschnittlich",
        "durchschnittlich",
        "überdurchschnittlich",
        "weit überdurchschnittlich",
    ],
    "demat5plus": [
        "weit unterdurchschnittlich",
        "unterdurchschnittlich",
        "durchschnittlich",
        "überdurchschnittlich",
        "weit überdurchschnittlich",
    ],
}


class KnowledgeBaseUnavailableError(Exception):
    """Raised when `config.KNOWLEDGE_BASE_DIR` does not exist. A normal
    state (ADR 0005 §9.6) — callers must show an explicit message, never
    crash and never fall back to a guessed band.
    """


def kb_available() -> bool:
    return config.KNOWLEDGE_BASE_DIR.exists()


def _run_lookup(args: list[str]) -> dict[str, Any]:
    if not kb_available():
        raise KnowledgeBaseUnavailableError(
            f"Knowledge base not found at {config.KNOWLEDGE_BASE_DIR}. "
            "Norm lookups are unavailable until it is present."
        )
    script = config.KNOWLEDGE_BASE_DIR / LOOKUP_SCRIPT
    if not script.exists():
        raise KnowledgeBaseUnavailableError(f"lookup script not found: {script}")

    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Exit 2 = refused (e.g. `druckfehler_vermutet` without
        # `--allow-flagged`), exit 1 = input error. `lookup.py` still
        # writes its `{"ok": false, "error": ...}` JSON body to stdout in
        # both cases (KB rule 3: report what is refused, never silently
        # work around it) — surface it in the exception message.
        raise RuntimeError(
            f"lookup.py {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stdout.strip() or proc.stderr}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"lookup.py {' '.join(args)} produced unparseable output: {proc.stdout!r}"
        ) from exc


def resolve_norm_group(
    norm_test: str, norm_variant: str | None
) -> tuple[str, str, str]:
    """(norm_group, label, norm_variant) — the group is the stored variant
    plus the `_gesamt` suffix where the test has gender-split tables.
    Nothing is derived from the test date (ADR 0010). Raises `ValueError`
    when the variant is missing or not published for this test.
    """
    if not norm_variants.is_valid(norm_test, norm_variant):
        valid = [v.key for v in norm_variants.variants_for(norm_test)]
        raise ValueError(
            f"no norm_variant recorded for norm_test={norm_test!r} "
            f"(norm_variant={norm_variant!r}); valid variants: {valid}"
        )
    assert norm_variant is not None
    group = (
        f"{norm_variant}_gesamt"
        if norm_variants.GENDER_SPLIT.get(norm_test, False)
        else norm_variant
    )
    label = norm_variants.label_for(norm_test, norm_variant) or norm_variant
    return group, label, norm_variant


_KLASSE_GRADE_RE = re.compile(r"^\s*(\d+)")


def grade_cross_check_warning(
    klasse: str | None, norm_test: str, norm_variant: str | None, norm_group: str
) -> str | None:
    """A disagreement between the label's leading grade digit and the
    expected grade for the chosen norm variant (ADR 0010). Never overrides
    the stored variant — only warns. Skipped without a warning if `klasse`
    has no leading digit, or when the variant has no expected grade (DEMAT
    5+ school types).
    """
    expected = norm_variants.expected_grade(norm_variant)
    if not klasse or expected is None:
        return None
    match = _KLASSE_GRADE_RE.match(klasse)
    if not match:
        return None
    label_grade = int(match.group(1))
    if label_grade == expected:
        return None
    return (
        f"Hinweis: Klassenbezeichnung ({klasse}) passt nicht zur gewählten "
        f"Normtabelle {norm_group} — bitte Klasse und Normtabelle prüfen."
    )


def classify(
    norm_test: str, *, pr: float | None = None, t: float | None = None
) -> dict:
    """`lookup.py classify --test <norm_test> (--pr X | --t X)`. Never
    raises on a wrong-basis call — the response carries `category: null`
    and a note; the caller must read `basis` from the response.
    """
    args = ["classify", "--test", norm_test]
    if pr is not None:
        args += ["--pr", str(pr)]
    elif t is not None:
        args += ["--t", str(t)]
    else:
        raise ValueError("classify() requires pr or t")
    return _run_lookup(args)


def norm(
    norm_test: str, scale: str, group: str, raw: float, *, allow_flagged: bool = False
) -> dict:
    """`lookup.py norm ...` — used to fill a norm value the sheet left for
    the evaluator but whose Rohwert it did carry (ADR 0013). Its only
    caller for this purpose is `demat.norm_completion`; `_check_norm_mapping`
    below also calls it, but only to check a value someone wrote by hand,
    never to store one.
    """
    args = [
        "norm",
        "--test",
        norm_test,
        "--scale",
        scale,
        "--group",
        group,
        "--raw",
        str(raw),
    ]
    if allow_flagged:
        args.append("--allow-flagged")
    return _run_lookup(args)


def class_norm(scale: str, rohwerte: list[float]) -> dict | None:
    """`lookup.py class --scale <SCALE> --scores <rw...>` — DEMAT 3+ only,
    structurally (the CLI's `class` subparser takes no `--test`; its
    `--scale` vocabulary is DEMAT 3+'s). `--scores` takes Rohwerte, never
    Prozentränge (spec §6.2.4). Needs >=2 scores; returns None otherwise.
    """
    if len(rohwerte) < 2:
        return None
    args = ["class", "--scale", scale, "--scores", *[str(r) for r in rohwerte]]
    return _run_lookup(args)


def basis_for(defn: TestDefinition) -> str:
    """'pr' | 't' — the version's classification basis (spec §6.2.2),
    read from `classify()`'s response rather than hardcoded, per version.
    """
    if defn.norm_test is None:
        raise ValueError(f"{defn.test_version} has no norm_test")
    result = classify(defn.norm_test, pr=0)
    return str(result["basis"])


def classifier(norm_test: str, basis: str) -> ClassifyFn:
    """A `ClassifyFn` bound to one `norm_test`/`basis` pair — the same
    shape `build_class_data()` used to build inline. `demat/reports
    /__init__.py` and the child-report pipeline both use this, so there is
    exactly one place that decides which scale a raw value is classified
    on.
    """

    def _classify(value: float | None) -> dict[str, Any]:
        if value is None:
            return {"category": None, "basis": basis}
        if basis == "pr":
            return classify(norm_test, pr=value)
        return classify(norm_test, t=value)

    return _classify


# --- Child report (ADR 0014 spec §6.3, §6.3a) --------------------------------

MEDIAN_CATEGORY = "durchschnittlich"

# DEMAT 5+ classifies only on T (`lookup.py` offers no PR scale for it), so
# its PR-scale "durchschnittlich" range cannot be probed via `classify()`.
# This is the single documented exception to "band edges only from the KB
# CLI" (spec §6.3a): T 40-60 <-> PR 15.8-84.5, per the manual's own Tabelle 3
# (DEMAT 5+ manual, S. 21; `demat5plus_diagnostik.md:222`). A test ties this
# constant to `band_segments("demat5plus")` still reporting a T
# "durchschnittlich" segment of 40-60, so a KB change is noticed.
DEMAT5PLUS_PR_AVERAGE: tuple[float, float] = (15.8, 84.5)

# Spec §6.3a: the PR-Band/T-Wert-Band confidence level printed next to each
# version's band label. 90 % only for DEMAT 5+ ("T-Wert-Band (90 %)");
# every other version's manual states 68 %.
CONFIDENCE_LEVEL: dict[str, str] = {
    "demat1plus": "68 %",
    "demat2plus": "68 %",
    "demat3plus": "68 %",
    "demat5plus": "90 %",
}


class BandSegment(BaseModel):
    """One collapsed run of consecutive probed values sharing a category
    (spec §6). `lo`/`hi` are the probed numeric bounds; `open_low`/
    `open_high` mark whichever end is unbounded in the manual's own wording
    (T-basis versions only: "<=29", ">=71") — the probe itself is always
    bounded (0-100), so `lo`/`hi` stay usable numbers even where the manual
    prints an open end.
    """

    lo: float
    hi: float
    category: str
    label: str
    open_low: bool = False
    open_high: bool = False


_PROBE_MIN = 0
_PROBE_MAX = 100


@functools.cache
def band_segments(norm_test: str) -> tuple[BandSegment, ...]:
    """Probe `classify()` across the full 0-100 scale and collapse the
    result into ordered, worst -> best `BandSegment`s (spec §6, §6.2.2).
    Never hardcodes a boundary. Cached per process (spec R-2: about 101
    subprocess calls per version) — `median_zone()` reuses this cache.
    """
    probe_basis = str(classify(norm_test, pr=0)["basis"])

    # Each probed value is either unambiguous (`category`) or, on a T-basis
    # version with shared boundary values (spec R-13's DEMAT 5+ case: the
    # manual prints overlapping ranges at 30/40/60/70), ambiguous: `category`
    # is `None` and `categories` names the two adjacent categories the
    # manual assigns that exact value to. An ambiguous value belongs to
    # *both* segments it borders — it is the shared edge, not a gap.
    points: list[tuple[int, str | None, list[str] | None]] = []
    for v in range(_PROBE_MIN, _PROBE_MAX + 1):
        result = (
            classify(norm_test, pr=v)
            if probe_basis == "pr"
            else classify(norm_test, t=v)
        )
        category = result.get("category")
        categories = result.get("categories")
        if category is None and not categories:
            continue
        points.append((v, category, categories))

    if not points:
        raise RuntimeError(f"{norm_test}: classify() produced no category over 0-100")

    runs: list[tuple[int, int, str]] = []
    first_v, first_cat, first_cats = points[0]
    run_start = first_v
    run_category = first_cat if first_cat is not None else first_cats[0]  # type: ignore[index]
    run_end = run_start
    for v, category, categories in points[1:]:
        if category == run_category:
            run_end = v
            continue
        if category is None and categories and run_category in categories:
            # Shared boundary: close the current run *at* this value, and
            # open the next run *at* the same value (spec R-13).
            runs.append((run_start, v, run_category))
            other = next(c for c in categories if c != run_category)
            run_start = v
            run_category = other
            run_end = v
            continue
        runs.append((run_start, run_end, run_category))
        run_start = run_end = v
        run_category = category if category is not None else categories[0]  # type: ignore[index]
    runs.append((run_start, run_end, run_category))

    result_segments: list[BandSegment] = []
    for i, (lo, hi, category) in enumerate(runs):
        open_low = i == 0
        open_high = i == len(runs) - 1
        if probe_basis == "pr":
            label = f"{lo:g}–{hi:g}"
        elif open_low:
            label = f"≤{hi:g}"
        elif open_high:
            label = f"≥{lo:g}"
        else:
            label = f"{lo:g}–{hi:g}"
        result_segments.append(
            BandSegment(
                lo=float(lo),
                hi=float(hi),
                category=category,
                label=label,
                open_low=open_low,
                open_high=open_high,
            )
        )
    return tuple(result_segments)


def median_zone(norm_test: str) -> tuple[float, float] | None:
    """The `(lo, hi)` PR-scale edges of the manual's "durchschnittlich"
    category (spec §6.3a), or `None`. Cached transitively via
    `band_segments()`. Depends only on the version, never on a child.

    - DEMAT 2+/3+: probed on PR directly.
    - DEMAT 5+: `lookup.py` offers no PR scale, so this returns the cited
      `DEMAT5PLUS_PR_AVERAGE` constant instead of probing.
    - DEMAT 1+: `None` — known asymmetry, the KB states no PR equivalent.
    """
    if norm_test == "demat1plus":
        return None
    if norm_test == "demat5plus":
        return DEMAT5PLUS_PR_AVERAGE
    for segment in band_segments(norm_test):
        if segment.category == MEDIAN_CATEGORY:
            return (segment.lo, segment.hi)
    return None


def gesamt_status_caveat(
    status: str | None, *, rw: float | None = None, table: str | None = None
) -> str | None:
    """A conditional Hinweis for a KB row status ADR 0013 would carry on
    the overall value's provenance (spec §6.6). `status` is one of
    `lookup.py`'s vocabulary (`unsicher`, `druckfehler_vermutet`) or `None`.

    ADR 0013's per-cell provenance columns are not in `schema.sql` yet
    (spec §12, "ADR 0013 provenance is an optional input") — callers that
    have no such column simply pass `status=None`, and this always returns
    `None`. Once ADR 0013 lands, its provenance rows feed `status`/`rw`
    /`table` here without a change to this function's contract.
    """
    if status is None:
        return None
    detail_parts = []
    if rw is not None:
        detail_parts.append(f"Rohwert {rw:g}")
    if table:
        detail_parts.append(f"Tabelle {table}")
    detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
    if status == "unsicher":
        return (
            f"Normwert zum Gesamtwert{detail} im Handbuch blass gedruckt "
            "(„unsicher“) – bei Entscheidungen im Handbuch prüfen."
        )
    if status == "druckfehler_vermutet":
        return (
            f"Normwert zum Gesamtwert{detail} gilt im Handbuch als Druckfehler "
            "vermutet – im gedruckten Original prüfen."
        )
    return None
