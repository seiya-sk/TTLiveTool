import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.report import prompt as prompt_module


def test_load_winning_patterns_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompt_module, "WINNING_PATTERNS_PATH", Path("criteria/winning_patterns.md"))

    assert prompt_module.load_winning_patterns() is None
    assert prompt_module.build_system_prompt() == prompt_module.SYSTEM_PROMPT


def test_load_winning_patterns_returns_none_when_file_is_blank(tmp_path, monkeypatch):
    criteria_dir = tmp_path / "criteria"
    criteria_dir.mkdir()
    (criteria_dir / "winning_patterns.md").write_text("   \n\n  ", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompt_module, "WINNING_PATTERNS_PATH", Path("criteria/winning_patterns.md"))

    assert prompt_module.load_winning_patterns() is None


def test_build_system_prompt_embeds_file_content_when_present(tmp_path, monkeypatch):
    criteria_dir = tmp_path / "criteria"
    criteria_dir.mkdir()
    (criteria_dir / "winning_patterns.md").write_text("## ギフト\n20時台に開始すると伸びやすい", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompt_module, "WINNING_PATTERNS_PATH", Path("criteria/winning_patterns.md"))

    loaded = prompt_module.load_winning_patterns()
    assert loaded == "## ギフト\n20時台に開始すると伸びやすい"

    system_prompt = prompt_module.build_system_prompt()
    assert prompt_module.SYSTEM_PROMPT in system_prompt
    assert "20時台に開始すると伸びやすい" in system_prompt
    assert "勝ちパターン" in system_prompt
