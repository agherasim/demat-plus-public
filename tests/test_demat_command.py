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


def test_launcher_path_includes_the_claude_cli_install_dir(tmp_path):
    assert _demat(tmp_path, "install-app").returncode == 0
    launcher = (
        tmp_path / "Apps" / "DEMAT+.app" / "Contents" / "MacOS" / "demat-launcher"
    ).read_text()
    assert "$HOME/.local/bin" in launcher


def test_setup_rejects_an_unknown_runner(tmp_path):
    result = _demat(tmp_path, "setup", "--runner", "nope")
    assert result.returncode == 1
    assert "direct or agent_sdk" in result.stderr


def _write_config(tmp_path: Path, runner: str) -> Path:
    # Sources only the shell functions (main is stripped), then calls
    # write_config the way cmd_setup does; no uv sync or app generation.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    script = DEMAT.read_text().replace('\nmain "$@"\n', "\n")
    subprocess.run(
        ["bash", "-c", f'{script}\nwrite_config 1 "{runner}"'],
        env={**os.environ, "DEMAT_HOME": str(home)},
        check=True,
        capture_output=True,
    )
    return home / "config.env"


def test_setup_writes_the_agent_sdk_runner_without_a_key(tmp_path):
    config_env = _write_config(tmp_path, "agent_sdk")
    text = config_env.read_text()
    assert "DEMAT_RUNNER=agent_sdk" in text
    assert "ANTHROPIC_API_KEY" not in text
    assert config_env.stat().st_mode & 0o777 == 0o600


def test_setup_runner_switch_keeps_the_rest_of_config_env(tmp_path):
    config_env = _write_config(tmp_path, "")
    config_env.write_text("DEMAT_RUNNER=direct\nANTHROPIC_API_KEY='k'\n")
    _write_config(tmp_path, "agent_sdk")
    lines = config_env.read_text().splitlines()
    assert "DEMAT_RUNNER=agent_sdk" in lines
    assert "DEMAT_RUNNER=direct" not in lines
    assert "ANTHROPIC_API_KEY='k'" in lines
    assert config_env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("runner", "key_seen"), [("agent_sdk", ""), ("direct", "k")])
def test_run_hides_the_api_key_from_the_cli_runner(tmp_path, runner, key_seen):
    # cmd_run up to the key handling: load_config, then the agent_sdk unset.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.env").write_text(f"DEMAT_RUNNER={runner}\nANTHROPIC_API_KEY=k\n")
    script = DEMAT.read_text().replace('\nmain "$@"\n', "\n")
    start = script.index("  load_config\n  # The CLI prefers")
    end = script.index('  local port="${DEMAT_PORT')
    body = script[start:end]
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'{script}\nf() {{\n{body}\n}}\nf; echo "[${{ANTHROPIC_API_KEY:-}}]"',
        ],
        env={**os.environ, "DEMAT_HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert out.strip() == f"[{key_seen}]"
