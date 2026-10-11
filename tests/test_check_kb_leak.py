"""`scripts/check_kb_leak.py` (ADR 0017) against a synthetic knowledge base
built in `tmp_path`. Every norm value, band and sentence below is invented —
a test quoting the real KB would itself be the leak the guard exists to stop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_kb_leak

_PR_LINE = "ARI PR 12 · SAR PR 9"
_NORMEN = """\
test,tabelle,normgruppe,skala,rohwert,pr,pr_band,t_wert,t_band,status,anmerkung,seite
demat2plus,X1,k3_gesamt,gesamtwert,12.0,41,33-52,47,43-51,ok,,1
demat2plus,X1,k3_gesamt,gesamtwert,13.0,47,38-57,49,45-53,ok,,1
demat2plus,X1,k3_gesamt,gesamtwert,14.0,53,44-63,51,47-55,ok,,1
demat3plus,X2,anfang_k4,subtest_geometrie,4,58,47-68,52,48-56,unsicher,Zelle im Handbuch sehr blass und kaum lesbar gedruckt worden,2
"""

_LOOKUP = """\
BANDS = {
    'demat2plus': ('pr', 'Handbuch', [(93, 100, 'ausgezeichnet'), (71, 92, 'gut'),
                   (30, 70, 'mittel'), (8, 29, 'schwach'), (0, 7, 'sehr schwach')]),
}
"""

_PROSE = (
    "Die Ergebnisse einzelner Untertests sollten stets nur zurückhaltend "
    "interpretiert und niemals isoliert betrachtet werden.\n"
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "data" / "demat2plus").mkdir(parents=True)
    (root / "data" / "demat2plus" / "normen.csv").write_text(_NORMEN, encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "lookup.py").write_text(_LOOKUP, encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "allgemein.md").write_text(_PROSE, encoding="utf-8")
    return root


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _run(kb: Path, repo: Path) -> list[str]:
    knowledge = check_kb_leak.load_kb(kb)
    findings = check_kb_leak.scan(repo, knowledge, check_kb_leak.tracked_files(repo))
    findings = check_kb_leak.filter_allowed(
        repo, findings, check_kb_leak.load_allowlist(repo)
    )
    return [f.detector for f in findings]


def test_clean_tree_passes(kb, tmp_path):
    repo = _repo(tmp_path, {"app.py": "PR = 50  # durchschnittlich\n"})
    assert _run(kb, repo) == []


def test_copied_kb_file_is_flagged_by_name_and_hash(kb, tmp_path):
    repo = _repo(tmp_path, {"vendor/normen.csv": _NORMEN, "x/copy.txt": _LOOKUP})
    assert _run(kb, repo) == ["file", "file"]


def test_copied_prose_is_flagged(kb, tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": f"Wie das Handbuch sagt: {_PROSE}"})
    assert "prose" in _run(kb, repo)


def test_copied_anmerkung_is_flagged(kb, tmp_path):
    text = "note: Zelle im Handbuch sehr blass und kaum lesbar gedruckt worden\n"
    assert "prose" in _run(kb, _repo(tmp_path, {"a.md": text}))


def test_abbreviation_runs_are_not_prose(kb, tmp_path):
    text = "demat 1 demat 2 demat 3 demat 5 zs ad su mu sr sz fl ls lu\n"
    assert _run(kb, _repo(tmp_path, {"a.md": text})) == []


def test_band_label_cluster_is_flagged(kb, tmp_path):
    text = 'LABELS = ["0–7", "8–29",\n "30–70", "71–92", "93–100"]\n'
    assert _run(kb, _repo(tmp_path, {"t.py": text})) == ["bands"]


def test_copied_bands_tuples_are_flagged(kb, tmp_path):
    text = "B = [\n(93, 100, 'a'),\n(71, 92, 'b'),\n(30, 70, 'c'),\n]\n"
    assert _run(kb, _repo(tmp_path, {"t.py": text})) == ["bands"]


def test_lookup_literal_with_real_value_is_flagged(kb, tmp_path):
    text = '{("demat2plus", "gesamtwert", "k3_gesamt", 13.0): {"pr": "47"}}\n'
    assert _run(kb, _repo(tmp_path, {"t.py": text})) == ["lookup"]


def test_lookup_literal_with_fabricated_value_passes(kb, tmp_path):
    text = '{("demat2plus", "gesamtwert", "k3_gesamt", 13.0): {"pr": "11"}}\n'
    assert _run(kb, _repo(tmp_path, {"t.py": text})) == []


def test_real_band_after_its_group_is_flagged(kb, tmp_path):
    text = 'variant = "k3"\n\nassert pr_band == "38-57"\n'
    assert _run(kb, _repo(tmp_path, {"t.py": text})) == ["group"]


def test_range_cluster_is_flagged(kb, tmp_path):
    text = "| looked up | 33-52 | 38-57 | 44-63 |\n"
    assert "ranges" in _run(kb, _repo(tmp_path, {"a.md": text}))


def test_pr_list_is_flagged_and_allowlist_clears_it(kb, tmp_path):
    repo = _repo(tmp_path, {"a.md": f"{_PR_LINE}\n"})
    assert _run(kb, repo) == ["pr-list"]
    (repo / ".kb-guard-allow").write_text(f"a.md:{_PR_LINE}\n", encoding="utf-8")
    assert _run(kb, repo) == []


def test_public_exclude_paths_are_not_scanned(kb, tmp_path):
    repo = _repo(
        tmp_path,
        {
            "docs/specs/s.md": f"{_PR_LINE}\n",
            ".public-exclude": "# private\ndocs/specs/\n",
        },
    )
    assert _run(kb, repo) == []


def test_main_exit_codes(kb, tmp_path, capsys):
    clean = _repo(tmp_path, {"a.py": "x = 1\n"})
    assert check_kb_leak.main(["--kb", str(kb), "--root", str(clean)]) == 0
    assert check_kb_leak.main(["--kb", str(tmp_path / "nope")]) == 2
    (clean / "b.md").write_text(f"{_PR_LINE}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clean, check=True)
    assert check_kb_leak.main(["--kb", str(kb), "--root", str(clean)]) == 1
    # Values never reach the output, only path:line and the detector.
    assert "PR 12" not in capsys.readouterr().out


def test_empty_or_partial_kb_fails_closed(kb, tmp_path):
    clean = _repo(tmp_path, {"a.py": "x = 1\n"})
    empty = tmp_path / "empty-kb"
    (empty / "data").mkdir(parents=True)
    assert check_kb_leak.main(["--kb", str(empty), "--root", str(clean)]) == 2
    args = ["--kb", str(kb), "--root", str(clean)]
    assert check_kb_leak.main([*args, "--require-tests", "demat2plus"]) == 0
    assert check_kb_leak.main([*args, "--require-tests", "demat2plus,demat5plus"]) == 2
