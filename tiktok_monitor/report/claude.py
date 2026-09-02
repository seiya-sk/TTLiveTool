"""Thin Anthropic API wrapper. Uses forced tool-use so the response is a
validated dict matching sections.py's schema, instead of freeform text that
would need fragile markdown/JSON parsing.
"""
import logging

import anthropic

from .sections import REPORT_SECTIONS

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000
TOOL_NAME = "submit_report_sections"


def build_tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "TikTokライブ配信分析レポートの各セクションを提出する。",
        "input_schema": {
            "type": "object",
            "properties": {s["key"]: s["schema"] for s in REPORT_SECTIONS},
            "required": [s["key"] for s in REPORT_SECTIONS],
            "additionalProperties": False,
        },
        "strict": True,
    }


def generate_sections(system_prompt: str, user_content: list[dict]) -> tuple[dict, dict]:
    """Calls Claude and returns (ai_sections, usage).

    ai_sections is a dict keyed by each section's `key` in sections.py.
    usage is a dict with input_tokens/output_tokens (plus cache token
    counts when applicable) for cost tracking. Raises RuntimeError if the
    model doesn't return the expected tool call (should not happen with
    tool_choice forcing it, but this is not something the caller should
    silently ignore)."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        tools=[build_tool()],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user_content}],
    )

    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_creation_input_tokens": message.usage.cache_creation_input_tokens,
        "cache_read_input_tokens": message.usage.cache_read_input_tokens,
    }
    logger.info(
        "Claude API usage (model=%s): input_tokens=%s output_tokens=%s "
        "cache_creation_input_tokens=%s cache_read_input_tokens=%s",
        MODEL,
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_creation_input_tokens"],
        usage["cache_read_input_tokens"],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return block.input, usage
    raise RuntimeError("Claude response did not include the expected tool_use block")
