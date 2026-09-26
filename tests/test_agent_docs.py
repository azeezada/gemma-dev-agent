from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agents_md_is_identical_to_claude_md():
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()
