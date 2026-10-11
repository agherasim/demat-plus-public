"""`packaging/demat` (ADR 0017), the command the Homebrew formula installs.
Only the subcommands that need no network or uv sync: those are exercised by
`demat setup` on a real install."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from demat import config

DEMAT = config.CODE_ROOT / "packaging" / "demat"


def _demat(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEMAT_HOME": str(tmp_path / "home"),
        "DEMAT_APP_PATH": str(tmp_path / "Apps" / "DEMAT+.app"),
        "DEMAT_BIN": "/opt/homebrew/opt/demat/bin/demat",
    }
    return subprocess.run(
        [str(DEMAT), *args], env=env, capture_output=True, text=True, check=False
    )


def test_uninstall_app_without_a_launcher_is_a_no_op(tmp_path):
    # The Homebrew formula's `test do` block relies on this exact message.
    result = _demat(tmp_path, "uninstall-app")
    assert result.returncode == 0
    assert "No DEMAT+ launcher" in result.stdout


def test_install_app_writes_a_launcher_that_runs_the_stable_command(tmp_path):
    assert _demat(tmp_path, "install-app").returncode == 0
    app = tmp_path / "Apps" / "DEMAT+.app"
    launcher = (app / "Contents" / "MacOS" / "demat-launcher").read_text()
    assert 'exec "/opt/homebrew/opt/demat/bin/demat" run --log' in launcher
    assert str(tmp_path / "home") in launcher
    assert "@@" not in launcher
    assert os.access(app / "Contents" / "MacOS" / "demat-launcher", os.X_OK)

    assert _demat(tmp_path, "uninstall-app").returncode == 0
    assert not app.exists()


@pytest.mark.parametrize("args", [("--help",), ("help",)])
def test_help_lists_the_subcommands(tmp_path, args):
    out = _demat(tmp_path, *args).stdout
    for sub in ("setup", "run", "install-app", "uninstall-app"):
        assert f"demat {sub}" in out


def test_unknown_command_fails(tmp_path):
    assert _demat(tmp_path, "nope").returncode == 1
