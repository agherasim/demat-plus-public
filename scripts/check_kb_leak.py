#!/usr/bin/env python3
"""Build-time guard: fail if any tracked file carries content from the private
DEMAT knowledge base (ADR 0017).

The knowledge base holds Hogrefe manual excerpts and norm tables, which are
copyrighted. This repo is mirrored to a public repository, so nothing derived
from the KB may be committed here — not whole files, not prose, not individual
RW -> PR/T lookups. The app reads the KB at runtime only.

The check runs in the private repo's CI, the one place that can see both this
tree and the KB, and it gates the public sync. It is a structural net, not a
proof: each detector targets a shape a leak has actually taken (or plausibly
would), and matches on structure rather than lone numbers so that `PR 0` in
prose does not trip it.

    file      a tracked file is byte-identical to, or named like, a KB data file
    prose     an 8-word run from KB prose (references, SKILL.md, annotations)
    bands     3+ of one test's manual interpretation band labels close together
    lookup    a `(test, skala, normgruppe, rw): {...}` literal whose values
              match the real KB row
    ranges    3+ distinct real PR/T band ranges (e.g. `12-34`) close together
    group     a real band range of a norm group within a few dozen lines after
              that group is named (e.g. `k3_gesamt`, then `pr_band == "a-b"`)
    pr-list   a line listing 2+ `PR <n>` values (looks like a lookup result)

Only the tree that would be published is scanned: tracked files minus
`.public-exclude`. Findings print `path:line detector [kb ref]`, never the matched values, so CI
logs stay clean. A deliberate, reviewed exception goes into
`.kb-guard-allow` as `path:line-text` (the exact stripped line).

    uv run python -m scripts.check_kb_leak --kb <demat-knowledge-base dir>
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SHINGLE_WORDS = 8
MIN_PROSE_WORDS = 5
WINDOW_LINES = 5
GROUP_REACH_LINES = 60
MIN_CLUSTER = 3
KB_DATA_NAMES = {"normen.csv", "tabellen.json", "PARSE_BERICHT.md"}
KB_DATA_PREFIX = "klassennormen_"
ALLOW_FILE = ".kb-guard-allow"
EXCLUDE_FILE = ".public-exclude"

_DASH = re.compile("[\u2010-\u2015\u2212]")  # hyphen..horizontal bar, minus
_WORD = re.compile(r"\w+", re.UNICODE)
_RANGE = re.compile(r"(?<![\d.,/])(\d{1,3})-(\d{1,3})(?![\d.,/])")
_PR_VALUE = re.compile(r"\bPR\s*\d{1,3}\b")
_LOOKUP_LITERAL = re.compile(
    r"\(\s*\"(demat\dplus)\"\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,\s*"
    r"([\d.]+)\s*\)\s*:\s*\{([^}]*)\}",
    re.DOTALL,
)
_SHOW_PROSE = False
_DICT_ITEM = re.compile(r"\"(pr|pr_band|t_wert|t_band)\"\s*:\s*\"([^\"]*)\"")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    detector: str
    ref: str = ""

    def __str__(self) -> str:
        ref = f" [{self.ref}]" if self.ref else ""
        return f"{self.path}:{self.line} {self.detector}{ref}"


@dataclass(frozen=True)
class NormRow:
    test: str
    tabelle: str
    normgruppe: str
    skala: str
    rohwert: float
    values: dict[str, str]

    @property
    def ref(self) -> str:
        return f"{self.test} {self.tabelle} {self.normgruppe}/{self.skala}"


@dataclass
class KnowledgeBase:
    file_hashes: set[str]
    shingles: set[tuple[str, ...]]
    band_labels: dict[str, set[str]]
    ranges: set[str]
    rows: dict[tuple[str, str, str, float], NormRow]
    group_ranges: dict[str, set[str]]


def _norm_dash(text: str) -> str:
    return _DASH.sub("-", text)


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(_norm_dash(text))]


def _shingles(words: list[str]) -> Iterator[tuple[str, ...]]:
    """Word runs that read as prose: at least 5 words of 4+ letters, so
    abbreviation lists (`zs ad su mu ...`) and `demat 1 demat 2 ...` don't
    count as copied text."""
    for i in range(len(words) - SHINGLE_WORDS + 1):
        run = tuple(words[i : i + SHINGLE_WORDS])
        if sum(len(w) >= 4 and w.isalpha() for w in run) >= MIN_PROSE_WORDS:
            yield run


def _band_labels(bands: object) -> dict[str, set[str]]:
    """Labels as `demat.reports.norms` renders them, from lookup.py's BANDS:
    `lo-hi`, or `<=hi` / `>=lo` for the open ends; plus the `(lo, hi` tuple
    form, for a copy of BANDS itself."""
    labels: dict[str, set[str]] = {}
    if not isinstance(bands, dict):
        return labels
    for test, spec in bands.items():
        out: set[str] = set()
        for lo, hi, _category in spec[2]:
            out.add(f"({lo}, {hi}")  # the BANDS tuple itself, copied as code
            if lo <= -999:
                out.add(f"≤{hi}")
            elif hi >= 999:
                out.add(f"≥{lo}")
            else:
                out.add(f"{lo}-{hi}")
        labels[test] = out
    return labels


def _lookup_bands(script: Path) -> object:
    """Read the BANDS literal without executing KB code."""
    if not script.exists():
        return {}
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BANDS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    return {}


def load_kb(kb: Path) -> KnowledgeBase:
    file_hashes: set[str] = set()
    shingles: set[tuple[str, ...]] = set()
    for path in kb.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = path.relative_to(kb)
        if rel.parts[0] not in ("data", "references", "scripts", "SKILL.md"):
            continue
        file_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        if path.suffix == ".md":
            shingles.update(_shingles(_words(path.read_text(encoding="utf-8"))))

    ranges: set[str] = set()
    group_ranges: dict[str, set[str]] = {}
    rows: dict[tuple[str, str, str, float], NormRow] = {}
    for csv_path in sorted((kb / "data").glob("*/normen.csv")):
        with csv_path.open(encoding="utf-8") as f:
            for raw in csv.DictReader(f):
                if raw.get("anmerkung"):
                    shingles.update(_shingles(_words(raw["anmerkung"])))
                for key in ("pr_band", "t_band"):
                    m = _RANGE.fullmatch(_norm_dash(raw.get(key) or ""))
                    if m and m.group(1) != m.group(2):
                        ranges.add(m.group(0))
                        for alias in _group_aliases(raw["normgruppe"]):
                            group_ranges.setdefault(alias, set()).add(m.group(0))
                try:
                    rw = float(raw["rohwert"])
                except ValueError:
                    continue
                row = NormRow(
                    test=raw["test"],
                    tabelle=raw["tabelle"],
                    normgruppe=raw["normgruppe"],
                    skala=raw["skala"],
                    rohwert=rw,
                    values={
                        k: _norm_dash(raw.get(k) or "")
                        for k in ("pr", "pr_band", "t_wert", "t_band")
                    },
                )
                rows[(row.test, row.skala, row.normgruppe, rw)] = row

    bands = _band_labels(_lookup_bands(kb / "scripts" / "lookup.py"))
    return KnowledgeBase(file_hashes, shingles, bands, ranges, rows, group_ranges)


def _group_aliases(group: str) -> set[str]:
    """`k3_gesamt` is also selected as variant `k3` (ADR 0010)."""
    aliases = {group}
    head = group.split("_", 1)[0]
    if re.fullmatch(r"k\d", head):
        aliases.add(head)
    return aliases


def public_pathspecs(root: Path) -> list[str]:
    """`.public-exclude` as git pathspecs: everything except the excluded
    paths. Shared with the publish workflow, so the guard scans exactly the
    tree that would be mirrored."""
    specs = ["."]
    exclude = root / EXCLUDE_FILE
    if exclude.exists():
        for line in exclude.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                specs.append(f":(exclude){line}")
    return specs


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *public_pathspecs(root)],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def _windows(lines: list[str]) -> Iterator[tuple[int, str]]:
    for i in range(len(lines)):
        yield i + 1, "\n".join(lines[i : i + WINDOW_LINES])


def _check_text(path: str, text: str, kb: KnowledgeBase) -> Iterable[Finding]:
    text = _norm_dash(text)
    lines = text.splitlines()

    # prose: one finding per line that starts a KB shingle.
    reported: set[int] = set()
    for lineno in range(1, len(lines) + 1):
        words = _words(" ".join(lines[lineno - 1 : lineno + 2]))
        hit = next((s for s in _shingles(words) if s in kb.shingles), None)
        if lineno not in reported and hit is not None:
            reported.update(range(lineno, lineno + 3))
            yield Finding(path, lineno, "prose", (_SHOW_PROSE and " ".join(hit)) or "")

    # bands / ranges: clusters inside a sliding window, reported once.
    last_band = last_range = -WINDOW_LINES
    for lineno, window in _windows(lines):
        for test, labels in kb.band_labels.items():
            hits = {lab for lab in labels if _label_in(lab, window)}
            if len(hits) >= MIN_CLUSTER and lineno > last_band + WINDOW_LINES:
                last_band = lineno
                yield Finding(path, lineno, "bands", test)
        hits = {m.group(0) for m in _RANGE.finditer(window)} & kb.ranges
        if len(hits) >= MIN_CLUSTER and lineno > last_range + WINDOW_LINES:
            last_range = lineno
            yield Finding(path, lineno, "ranges")

    reach_until: dict[str, int] = {}
    last_group = -1
    for lineno, line in enumerate(lines, 1):
        for alias in kb.group_ranges:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", line):
                reach_until[alias] = lineno + GROUP_REACH_LINES
        live = {a for a, until in reach_until.items() if until >= lineno}
        hits = {m.group(0) for m in _RANGE.finditer(line)}
        if (
            live
            and lineno != last_group
            and any(hits & kb.group_ranges[a] for a in live)
        ):
            last_group = lineno
            yield Finding(path, lineno, "group")

    for lineno, line in enumerate(lines, 1):
        if len(_PR_VALUE.findall(line)) >= 2:
            yield Finding(path, lineno, "pr-list")

    for m in _LOOKUP_LITERAL.finditer(text):
        test, skala, group, rw, body = m.groups()
        row = kb.rows.get((test, skala, group, float(rw)))
        if row is None:
            continue
        for field, value in _DICT_ITEM.findall(body):
            if value and value == row.values.get(field):
                lineno = text.count("\n", 0, m.start()) + 1
                yield Finding(path, lineno, "lookup", f"{row.ref} {field}")


def _label_in(label: str, window: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(label)}(?![\d.])", window) is not None


def scan(root: Path, kb: KnowledgeBase, files: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files:
        if rel == ALLOW_FILE:
            continue
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        name = path.name
        if (
            hashlib.sha256(data).hexdigest() in kb.file_hashes
            or name in KB_DATA_NAMES
            or name.startswith(KB_DATA_PREFIX)
        ):
            findings.append(Finding(rel, 1, "file"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_check_text(rel, text, kb))
    return findings


def load_allowlist(root: Path) -> set[tuple[str, str]]:
    allow = root / ALLOW_FILE
    if not allow.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in allow.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        path, _, text = line.partition(":")
        out.add((path, _norm_dash(text.strip())))
    return out


def filter_allowed(
    root: Path, findings: list[Finding], allow: set[tuple[str, str]]
) -> list[Finding]:
    kept: list[Finding] = []
    for f in findings:
        lines = _norm_dash((root / f.path).read_text(encoding="utf-8")).splitlines()
        text = lines[f.line - 1].strip() if f.line <= len(lines) else ""
        if (f.path, text) not in allow:
            kept.append(f)
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--require-tests",
        default="",
        help="comma-separated test keys whose norm rows must load (CI: all four)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="also print the flagged line (local review only, never in CI)",
    )
    args = parser.parse_args(argv)
    global _SHOW_PROSE
    _SHOW_PROSE = args.show

    if not (args.kb / "data").is_dir():
        print(f"error: no knowledge base at {args.kb}", file=sys.stderr)
        return 2
    kb = load_kb(args.kb)
    # Fail closed: a KB that loads as empty (moved files, renamed columns)
    # would otherwise make every tree look clean.
    loaded = {row.test for row in kb.rows.values()}
    required = {t for t in args.require_tests.split(",") if t}
    print(
        f"knowledge base: {len(kb.rows)} norm rows ({', '.join(sorted(loaded))}), "
        f"{len(kb.band_labels)} band sets, {len(kb.shingles)} prose shingles"
    )
    if not (kb.rows and kb.band_labels and kb.shingles) or required - loaded:
        print(
            "error: knowledge base loaded incompletely; refusing to vouch for the "
            f"tree (missing: {sorted(required - loaded) or 'rows/bands/prose'})",
            file=sys.stderr,
        )
        return 2
    root = args.root.resolve()
    findings = scan(root, kb, tracked_files(root))
    findings = filter_allowed(root, findings, load_allowlist(root))
    for f in findings:
        print(f)
        if args.show:
            lines = (root / f.path).read_text(encoding="utf-8").splitlines()
            print(f"    {lines[f.line - 1].strip()[:160]}")
    if findings:
        print(
            f"\n{len(findings)} possible knowledge-base leak(s). Remove the content, "
            f"or add a reviewed exception to {ALLOW_FILE}.",
            file=sys.stderr,
        )
        return 1
    print("no knowledge-base content found in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
