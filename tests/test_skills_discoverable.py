"""`config.ensure_skills_discoverable` (ADR 0017 §8): the CLI runner's skill
links live in a dedicated dir under the data home, never in the code dir."""

from __future__ import annotations

from demat import config


def test_skill_links_live_under_the_data_home_and_resolve():
    config.ensure_skills_discoverable()
    assert config.CLAUDE_SKILLS_LINK.is_relative_to(config.AGENT_WORKDIR)
    assert config.AGENT_WORKDIR != config.DEMAT_HOME.resolve()
    assert not config.AGENT_WORKDIR.is_relative_to(config.CODE_ROOT)
    links = list(config.CLAUDE_SKILLS_LINK.iterdir())
    assert links
    for link in links:
        assert link.is_symlink()
        assert (link / "SKILL.md").is_file()
        assert link.resolve().is_relative_to(config.SKILLS_DIR.resolve())
