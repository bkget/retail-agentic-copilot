"""Only tests the parts of GeminiADKProvider that don't require a live API key: the
markdown-fence stripping helper, the classifier-reply parser, and the fail-fast
behavior when no key is configured. Actual generation against the real Gemini API is
untested in this environment - see the module docstring in app/agent/gemini_provider.py.
"""

import os

import pytest

from app.agent.gemini_provider import (
    GeminiADKProvider,
    _parse_classifier_reply,
    _strip_markdown_fence,
)
from app.agent.llm_provider import Intent
from app.config import get_settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("  SELECT 1  ", "SELECT 1"),
    ],
)
def test_strip_markdown_fence(raw, expected):
    assert _strip_markdown_fence(raw) == expected


@pytest.mark.parametrize(
    "reply,expected_intent",
    [
        ("GREETING: Hi there!", Intent.GREETING),
        ("HELP: You can ask about revenue...", Intent.HELP),
        ("CLARIFY: Which time period?", Intent.CLARIFICATION_NEEDED),
        ("EXPLAIN", Intent.EXPLAIN_PREVIOUS),
        ("explain", Intent.EXPLAIN_PREVIOUS),
        ("COVERAGE", Intent.DATA_COVERAGE),
        ("coverage", Intent.DATA_COVERAGE),
        ("QUERY", Intent.QUERY),
        ("  query  ", Intent.QUERY),
        ("something the model said that doesn't match any prefix", Intent.QUERY),
    ],
)
def test_parse_classifier_reply(reply, expected_intent):
    result = _parse_classifier_reply(reply)
    assert result.intent == expected_intent


def test_parse_classifier_reply_extracts_text_after_prefix():
    result = _parse_classifier_reply("CLARIFY: Which time period would you like?")
    assert result.clarifying_question == "Which time period would you like?"


def test_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_FILE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            GeminiADKProvider()
    finally:
        get_settings.cache_clear()
