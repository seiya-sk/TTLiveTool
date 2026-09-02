import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.report import claude as claude_module


def make_fake_message(input_tokens=1234, output_tokens=567):
    tool_use_block = SimpleNamespace(
        type="tool_use",
        name=claude_module.TOOL_NAME,
        input={"viewer_highlights": "x", "comment_trends": "y", "visual_feedback": "z", "next_stream_suggestions": ["a"]},
    )
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    return SimpleNamespace(content=[tool_use_block], usage=usage)


@patch("tiktok_monitor.report.claude.anthropic.Anthropic")
def test_generate_sections_returns_usage_dict(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = make_fake_message(input_tokens=1000, output_tokens=250)
    mock_anthropic_cls.return_value = mock_client

    ai_sections, usage = claude_module.generate_sections("system prompt", [{"type": "text", "text": "data"}])

    assert ai_sections["viewer_highlights"] == "x"
    assert usage == {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }


@patch("tiktok_monitor.report.claude.anthropic.Anthropic")
def test_generate_sections_logs_usage(mock_anthropic_cls, caplog):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = make_fake_message(input_tokens=42, output_tokens=7)
    mock_anthropic_cls.return_value = mock_client

    with caplog.at_level("INFO", logger="tiktok_monitor.report.claude"):
        claude_module.generate_sections("system prompt", [{"type": "text", "text": "data"}])

    assert any("input_tokens=42" in r.message and "output_tokens=7" in r.message for r in caplog.records)


@patch("tiktok_monitor.report.claude.anthropic.Anthropic")
def test_generate_sections_raises_when_no_tool_use_block(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="oops")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=None, cache_read_input_tokens=None),
    )
    mock_anthropic_cls.return_value = mock_client

    try:
        claude_module.generate_sections("system prompt", [{"type": "text", "text": "data"}])
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
