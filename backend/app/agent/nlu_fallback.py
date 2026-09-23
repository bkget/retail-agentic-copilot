"""Optional LLM fallback for natural-language understanding - never for SQL.

Architecture choice (the "semantic layer + constrained NLU" pattern): the rule-based
mapper is fast, free and deterministic, and handles the phrasings it knows. Only when
it can't make sense of a message does the orchestrator ask an LLM to *rewrite* that
message into a canonical question built strictly from the known vocabulary (or to
declare it out of scope). The rewrite is then fed back through the same deterministic
mapper -> SQL builder -> AST guardrail pipeline, so an LLM hallucination can at worst
produce a clarifying question, never an unvalidated query or an invented number.

Works with any OpenAI-compatible Chat Completions endpoint, which covers the free
options without a paid key:
  * Ollama (local, free):      NLU_BASE_URL=http://host.docker.internal:11434/v1  NLU_MODEL=qwen2.5:3b
  * Groq free tier:            NLU_BASE_URL=https://api.groq.com/openai/v1         NLU_MODEL=llama-3.1-8b-instant
  * Gemini free tier:          NLU_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai  NLU_MODEL=gemini-flash-lite-latest
  * OpenRouter ":free" models: NLU_BASE_URL=https://openrouter.ai/api/v1
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.agent.llm_provider import ConversationTurn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RewriteResult:
    in_scope: bool
    rewritten: str | None
    reason: str | None = None


class QuestionRewriter:
    """Interface. `rewrite` must never raise - a failed/slow LLM call degrades to the
    deterministic behavior (returns None), it doesn't break the turn."""

    name = "none"

    async def rewrite(
        self, question: str, history: list[ConversationTurn], year_range: str | None
    ) -> RewriteResult | None:  # pragma: no cover - interface
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


SYSTEM_PROMPT = """You rewrite user messages for a retail sales analytics assistant.
The assistant can ONLY answer questions built from this vocabulary:
- Measures: total revenue, units sold, number of transactions, average order value
- Breakdowns ("by ..."): division, district, item, supplier, manufacturer country, payment type, bank, year, quarter, month
- Filters: a specific year ({years}), "top N", or a named value in single quotes (e.g. 'DHAKA', 'cash')
There is NO data about customers, profit/cost, staff, inventory, forecasts, or anything outside retail sales.

Reply with ONLY a JSON object, no prose:
{{"in_scope": true|false, "rewritten": "<question>" or null, "reason": "<short reason>"}}
- If the message can be answered (possibly using the recent conversation for context), set in_scope=true and
  rewrite it as ONE short, explicit question using only the vocabulary above,
  e.g. "Total revenue by district and month in 2020" or "Top 5 items by units sold".
- If it cannot be answered from this data, set in_scope=false and rewritten=null."""


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_rewrite_reply(text: str) -> RewriteResult | None:
    data = _extract_json(text)
    if not isinstance(data, dict) or "in_scope" not in data:
        return None
    rewritten = data.get("rewritten")
    if rewritten is not None and (not isinstance(rewritten, str) or len(rewritten) > 300):
        rewritten = None
    return RewriteResult(
        in_scope=bool(data.get("in_scope")) and bool(rewritten),
        rewritten=rewritten.strip() if rewritten else None,
        reason=str(data.get("reason"))[:200] if data.get("reason") else None,
    )


class OpenAICompatibleRewriter(QuestionRewriter):
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str | None, timeout_seconds: float = 20.0):
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds
        )
        self._model = model

    async def rewrite(
        self, question: str, history: list[ConversationTurn], year_range: str | None
    ) -> RewriteResult | None:
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(years=year_range or "see data")}]
        for turn in history[-3:]:
            messages.append({"role": "user", "content": turn.question})
            if turn.narrative_text:
                messages.append({"role": "assistant", "content": turn.narrative_text[:400]})
        messages.append({"role": "user", "content": question})
        try:
            resp = await self._client.post(
                "/chat/completions",
                json={"model": self._model, "messages": messages, "temperature": 0, "max_tokens": 150},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:  # network, quota, bad payload - degrade gracefully
            logger.warning("NLU fallback unavailable: %s", type(exc).__name__)
            return None
        return parse_rewrite_reply(content)

    async def close(self) -> None:
        await self._client.aclose()


def create_rewriter(settings) -> QuestionRewriter | None:
    if settings.nlu_fallback != "openai_compatible":
        return None
    return OpenAICompatibleRewriter(
        base_url=settings.nlu_base_url,
        model=settings.nlu_model,
        api_key=settings.nlu_api_key,
        timeout_seconds=settings.nlu_timeout_seconds,
    )
