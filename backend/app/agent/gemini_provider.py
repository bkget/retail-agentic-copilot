"""Real Gemini implementation of LLMProvider, via Google ADK.

Only imported when LLM_PROVIDER=gemini (see app/main.py's deferred import) - `mock` mode
never touches google-adk, so it stays a true optional dependency.

Honesty note: the ADK call shape below (LlmAgent / Runner / InMemorySessionService /
run_async / Event.is_final_response) was verified against the actually-installed
google-adk package (introspected its real signatures/fields directly, not guessed from
memory) at the time this was written. What has NOT been exercised is a live call against
the real Gemini API - there is no API key available in this environment. Treat first use
against a real key as the actual integration test; the orchestrator/guardrail/tests
around this provider are already proven independent of it (see MockLLMProvider).
"""

from __future__ import annotations

import uuid

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent.llm_provider import (
    ConversationTurn,
    Intent,
    IntentResult,
    LLMGenerationError,
    LLMProvider,
)
from app.config import get_settings
from app.schema.catalog import SchemaCatalog

APP_NAME = "agentic-data-copilot"
USER_ID = "agentic-data-copilot-backend"

SYSTEM_INSTRUCTION = (
    "You are a PostgreSQL query generation assistant for a sales analytics semantic "
    "layer. Given a schema description and a user's natural-language question, output "
    "ONLY a single valid PostgreSQL SELECT statement - no markdown code fences, no "
    "explanation, no trailing semicolon. The statement will be independently validated "
    "against a strict AST allow-list before execution (only the two tables and the "
    "functions listed in the schema are permitted), so do not try to work around any "
    "restriction. If a previous attempt's error is provided, fix that specific problem. "
    "This is only ever called for genuine analytical questions - a separate step already "
    "handled greetings, help requests, and vague/ambiguous questions before reaching you."
)

CLASSIFIER_INSTRUCTION = (
    "You are the front door of a sales analytics assistant. Given the user's message and "
    "recent conversation history, decide what to do and reply with EXACTLY one of these "
    "six forms and nothing else:\n"
    "- GREETING: <a short, friendly reply offering to help with revenue, sales, products, "
    "stores, payments, suppliers, or business trends> - for greetings/social messages "
    "(hi, hello, thanks, bye, etc.)\n"
    "- HELP: <a concise overview of queryable topics - revenue and sales, units sold and "
    "transaction counts, stores/divisions/districts, products and suppliers, manufacturer "
    "countries, payment types and banks, time trends by year/quarter/month, "
    "top-performing items/suppliers/divisions - with 1-2 example questions. Never mention "
    "customers, that data is not available> - for \"what can you do\" style questions\n"
    "- CLARIFY: <a short clarifying question - e.g. about time period if a metric was "
    "named but nothing else qualifies it, or, if the question names no recognizable "
    "metric/dimension at all, a short note on what topics you CAN answer instead> - for "
    "anything that can't be answered from this schema on its own, as long as there's no "
    "prior turn it could be refining\n"
    "- EXPLAIN - when the user is asking you to explain, summarize, or restate the "
    "previous answer in different words (e.g. \"explain that in plain English\") AND "
    "there IS a previous turn in the conversation history to explain\n"
    "- COVERAGE - when the user is asking what time range / which years the data "
    "covers (a meta-question about the dataset itself, not an analytical question)\n"
    "- QUERY - for any genuine, answerable analytical question, including a short "
    "follow-up that refines a previous turn (e.g. \"only for 2024\"), and including "
    "requests to simply list/enumerate values (e.g. \"what products are available\") "
    "rather than rank them by a metric"
)


class GeminiADKProvider(LLMProvider):
    def __init__(self, model_name: str | None = None):
        import os

        settings = get_settings()
        api_key = settings.gemini_api_key
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY(_FILE) is not set.")
        # ADK's default (non-Vertex) Gemini model wrapper authenticates via this env var.
        os.environ.setdefault("GOOGLE_API_KEY", api_key)

        model = model_name or settings.gemini_model
        self._agent = LlmAgent(
            name="sql_generator",
            model=model,
            description="Generates PostgreSQL SELECT statements against a fixed sales semantic layer.",
            instruction=SYSTEM_INSTRUCTION,
        )
        self._classifier_agent = LlmAgent(
            name="intent_classifier",
            model=model,
            description="Decides whether a message needs a database query, a greeting, help, or clarification.",
            instruction=CLASSIFIER_INSTRUCTION,
        )
        self._session_service = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent, app_name=APP_NAME, session_service=self._session_service
        )
        self._classifier_runner = Runner(
            agent=self._classifier_agent, app_name=APP_NAME, session_service=self._session_service
        )

    async def classify(self, question: str, history: list[ConversationTurn]) -> IntentResult:
        prompt = self._build_classifier_prompt(question, history)
        reply = await self._run_single_turn(self._classifier_runner, prompt)
        return _parse_classifier_reply(reply)

    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        history: list[ConversationTurn],
        error_feedback: str | None = None,
    ) -> str:
        prompt = self._build_prompt(question, catalog.as_prompt_context(), history, error_feedback)
        reply = await self._run_single_turn(self._runner, prompt)
        return _strip_markdown_fence(reply)

    async def _run_single_turn(self, runner: Runner, prompt: str) -> str:
        """Each call gets its own session - conversation history is already threaded
        through the prompt text (see _build_prompt/_build_classifier_prompt), so there's
        no need to lean on ADK's own session/memory machinery for it. Both runners share
        self._session_service (Runner has no public `.session_service` accessor -
        verified against the installed package, not assumed)."""
        session_id = uuid.uuid4().hex
        await self._session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )

        message = types.Content(role="user", parts=[types.Part(text=prompt)])

        final_text: str | None = None
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session_id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(part.text or "" for part in event.content.parts)

        if final_text is None:
            raise LLMGenerationError("Gemini returned no final response for this turn.")
        return final_text.strip()

    @staticmethod
    def _build_classifier_prompt(question: str, history: list[ConversationTurn]) -> str:
        lines = [f"User message: {question}"]
        if history:
            lines.append("")
            lines.append("Recent conversation turns:")
            for turn in history[-3:]:
                lines.append(f"- Q: {turn.question}")
                if turn.sql:
                    lines.append(f"  SQL used: {turn.sql}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(
        question: str,
        schema_context: str,
        history: list[ConversationTurn],
        error_feedback: str | None,
    ) -> str:
        lines = [f"Schema:\n{schema_context}", "", f"Question: {question}"]

        if history:
            lines.append("")
            lines.append("Recent conversation turns (for follow-up context):")
            for turn in history[-3:]:
                lines.append(f"- Q: {turn.question}")
                if turn.sql:
                    lines.append(f"  SQL used: {turn.sql}")

        if error_feedback:
            lines.append("")
            lines.append(f"The previous attempt for THIS question failed: {error_feedback}")
            lines.append("Correct that specific problem in the new query.")

        return "\n".join(lines)


def _parse_classifier_reply(text: str) -> IntentResult:
    """Parses the classifier agent's GREETING:/HELP:/CLARIFY:/EXPLAIN/COVERAGE/QUERY
    reply. Standalone (not a method) so it's unit-testable without a live model call -
    see tests/test_gemini_provider.py."""
    text = text.strip()
    upper = text.upper()
    if upper.startswith("GREETING:"):
        return IntentResult(intent=Intent.GREETING, reply=text.split(":", 1)[1].strip())
    if upper.startswith("HELP:"):
        return IntentResult(intent=Intent.HELP, reply=text.split(":", 1)[1].strip())
    if upper.startswith("CLARIFY:"):
        return IntentResult(
            intent=Intent.CLARIFICATION_NEEDED, clarifying_question=text.split(":", 1)[1].strip()
        )
    if upper.startswith("EXPLAIN"):
        return IntentResult(intent=Intent.EXPLAIN_PREVIOUS)
    if upper.startswith("COVERAGE"):
        return IntentResult(intent=Intent.DATA_COVERAGE)
    # Anything else (including a bare "QUERY") falls through to a real query attempt -
    # failing open to "try to answer" is safer than failing open to "refuse to answer".
    return IntentResult(intent=Intent.QUERY)


def _strip_markdown_fence(text: str) -> str:
    """Gemini sometimes wraps SQL in a ```sql ... ``` fence despite instructions not to -
    strip it defensively rather than letting the guardrail reject valid SQL over formatting."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else ""
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()
