"""Only tests the parts of GeminiADKProvider that don't require a live API key: the
markdown-fence stripping helper, the classifier-reply parser, the mapper-reply parser,
and the fail-fast behavior when no key is configured. Actual generation against the
real Gemini API is untested in this environment - see the module docstring in
app/agent/gemini_provider.py.
"""

import pytest

from app.agent.gemini_provider import (
    GeminiADKProvider,
    _parse_classifier_reply,
    _parse_mapper_reply,
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
        ("GREETING", Intent.GREETING),
        ("greeting", Intent.GREETING),
        ("SCHEMA_INFO", Intent.SCHEMA_INFO),
        ("CLARIFICATION", Intent.CLARIFICATION),
        ("UNKNOWN", Intent.UNKNOWN),
        ("DATABASE_QUERY", Intent.DATABASE_QUERY),
        ("  database_query  ", Intent.DATABASE_QUERY),
        ("DATABASE_QUERY.", Intent.DATABASE_QUERY),
        ("something the model said that doesn't match any label", Intent.DATABASE_QUERY),
    ],
)
def test_parse_classifier_reply(reply, expected_intent):
    assert _parse_classifier_reply(reply) == expected_intent


@pytest.mark.parametrize(
    "reply,expected_metric,expected_dimension",
    [
        ("METRIC: total_revenue\nDIMENSION: store_division", "total_revenue", "store_division"),
        ("METRIC: total_revenue\nDIMENSION: NONE", "total_revenue", None),
        ("METRIC: NONE\nDIMENSION: NONE", None, None),
        ("metric: avg_revenue\ndimension: store_district", "avg_revenue", "store_district"),
        # Out-of-vocabulary values are rejected, not passed through - this is the
        # closed-vocabulary validation the mapper prompt relies on to avoid hallucinated
        # column names ever reaching SQL generation.
        ("METRIC: made_up_alias\nDIMENSION: store_division", None, "store_division"),
        ("not the expected format at all", None, None),
    ],
)
def test_parse_mapper_reply(reply, expected_metric, expected_dimension):
    metric, dimension = _parse_mapper_reply(reply)
    assert metric == expected_metric
    assert dimension == expected_dimension


def test_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_FILE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            GeminiADKProvider()
    finally:
        get_settings.cache_clear()
