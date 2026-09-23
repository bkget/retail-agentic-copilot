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

Shares three helpers with MockLLMProvider rather than re-implementing them: shape
detection (_classify_query_shape/_is_listing_intent), the reference-previous phrase
list, and _extract_named_filters. These are pure, deterministic, already-tested
functions - re-asking an LLM to reproduce "does this question quote a real division
name from the live database" only adds a hallucination surface with no upside, so
named-value filter matching in particular stays deterministic even in this provider.
What genuinely goes to the model: the GREETING/DATABASE_QUERY/SCHEMA_INFO/CLARIFICATION/
UNKNOWN judgment call, and the metric/dimension mapping (each constrained to a closed,
validated vocabulary - an unparseable or out-of-vocabulary reply is treated the same as
"the model didn't answer", never guessed past).
"""

from __future__ import annotations

import uuid

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types

from app.agent.llm_provider import (
    _ALL_TIME_RE,
    _DIMENSION_SYNONYMS,
    _METRIC_SYNONYMS,
    _REFERENCES_PREVIOUS_PHRASES,
    _TOP_N_RE,
    _any_word_in,
    _classify_query_shape,
    _extract_named_filters,
    _extract_years,
    _is_listing_intent,
    ClassificationResult,
    ColumnMapping,
    ExtractedSignals,
    Intent,
    LLMGenerationError,
    LLMProvider,
    ResolvedQuery,
    TermMappingResult,
)
from app.config import get_settings
from app.schema.catalog import SchemaCatalog

APP_NAME = "agentic-data-copilot"
USER_ID = "agentic-data-copilot-backend"

SYSTEM_INSTRUCTION = (
    "You are a PostgreSQL query generation assistant for a sales analytics semantic "
    "layer. Given a schema description and an already-resolved query description "
    "(metric, optional breakdown dimension, filters, year, top-N), output ONLY a "
    "single valid PostgreSQL SELECT statement - no markdown code fences, no "
    "explanation, no trailing semicolon. The statement will be independently validated "
    "against a strict AST allow-list before execution (only the two tables and the "
    "functions listed in the schema are permitted), so do not try to work around any "
    "restriction. If a previous attempt's error is provided, fix that specific problem. "
    "This is only ever called for an already-classified, already-mapped analytical "
    "question - a separate step already handled intent and column resolution.\n\n"
    "IMPORTANT: the given metric name (e.g. 'total_revenue') is a semantic label, NOT "
    "necessarily a literal column name - always look up the real column to use in the "
    "Schema section for whichever table you choose. The two tables name the same "
    "concept differently: mv_sales_daily_rollup (Tier 1, pre-aggregated) uses "
    "total_revenue and total_units_sold; mv_sales_analysis (Tier 2, row-level) uses "
    "total_price (per-row amount, needs SUM()) and quantity. Never guess a column name "
    "that isn't literally listed in the Schema section for the table you're querying."
)

CLASSIFIER_INSTRUCTION = (
    "You are the front door of a sales analytics assistant. Given a single user "
    "message (no conversation history - that's handled elsewhere), decide what kind "
    "of message it is and reply with EXACTLY one word, nothing else:\n"
    "- GREETING - a greeting or social message (hi, hello, thanks, bye, etc.)\n"
    "- SCHEMA_INFO - a question about what the assistant can do, what data/columns "
    "are available, or what time range the data covers - not an analytical question\n"
    "- DATABASE_QUERY - a genuine, answerable analytical question about the sales "
    "data (including a short follow-up like \"only for 2024\" or a reference to a "
    "prior answer like \"same as before but by district\" or \"explain that\"), or a "
    "request to list/enumerate values (e.g. \"what products are available\")\n"
    "- CLARIFICATION - names a metric (e.g. \"revenue\") but nothing else qualifies "
    "it (no breakdown, filter, time period, or \"total\"/\"average\") - an incomplete "
    "fragment, not a full question\n"
    "- UNKNOWN - names no recognizable metric, dimension, or reference to a prior "
    "answer at all"
)

MAPPER_INSTRUCTION = (
    "Map the user's question to a metric and, optionally, a breakdown dimension - "
    "ONLY from the closed lists of options given in the prompt, using their exact "
    "identifiers (e.g. 'total_revenue', 'store_division'). Reply on exactly two "
    "lines and nothing else:\n"
    "METRIC: <option-or-NONE>\n"
    "DIMENSION: <option-or-NONE>\n"
    "Use NONE for either line if the question doesn't clearly name that slot - never "
    "guess or invent an identifier that isn't in the given lists."
)

_METRIC_ALIASES = frozenset(alias for alias, _ in _METRIC_SYNONYMS)
_DIMENSION_COLUMNS = frozenset(column for column, _ in _DIMENSION_SYNONYMS)

# Tried in order, starting from whatever's configured (settings.gemini_model /
# model_name). All three were verified live against a real API key (a direct
# generate_content call, not just Google's models.list() - which had listed a model
# as available that then 404'd for this key) after gemini-2.0-flash was retired,
# gemini-3.6-flash's free tier turned out to only allow 20 requests, and
# gemini-2.5-flash-lite turned out to be closed to new API keys entirely. Kept as a
# fallback chain (not just a single "best" pick) so a quota limit on whichever model is
# primary doesn't have to mean the whole feature stops answering.
_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
)

_ROLE_AGENT_ATTR = {"classifier": "_classifier_agent", "mapper": "_mapper_agent", "generator": "_agent"}
_ROLE_RUNNER_ATTR = {"classifier": "_classifier_runner", "mapper": "_mapper_runner", "generator": "_runner"}


class GeminiADKProvider(LLMProvider):
    def __init__(self, model_name: str | None = None):
        import os

        settings = get_settings()
        api_key = settings.gemini_api_key
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY(_FILE) is not set.")
        # ADK's default (non-Vertex) Gemini model wrapper authenticates via this env var.
        os.environ.setdefault("GOOGLE_API_KEY", api_key)

        primary = model_name or settings.gemini_model
        # Whatever's configured stays first; the fallback models fill in behind it,
        # de-duplicated so a configured model that's already in the list doesn't
        # appear twice.
        self._models: tuple[str, ...] = (primary,) + tuple(
            m for m in _FALLBACK_MODELS if m != primary
        )
        self._model_index = 0
        self._session_service = InMemorySessionService()
        self._build_agents(self._models[0])

    def _build_agents(self, model: str) -> None:
        """(Re)builds all three role agents/runners against `model` - called once at
        init, and again by _run_single_turn whenever the current model hits a quota
        error and there's a next model in the fallback chain to switch to."""
        self._agent = LlmAgent(
            name="sql_generator",
            model=model,
            description="Generates PostgreSQL SELECT statements against a fixed sales semantic layer.",
            instruction=SYSTEM_INSTRUCTION,
        )
        self._classifier_agent = LlmAgent(
            name="intent_classifier",
            model=model,
            description="Decides whether a message is a greeting, schema question, query, clarification, or unknown.",
            instruction=CLASSIFIER_INSTRUCTION,
        )
        self._mapper_agent = LlmAgent(
            name="term_mapper",
            model=model,
            description="Maps a question's words to a closed vocabulary of metric/dimension identifiers.",
            instruction=MAPPER_INSTRUCTION,
        )
        self._runner = Runner(
            agent=self._agent, app_name=APP_NAME, session_service=self._session_service
        )
        self._classifier_runner = Runner(
            agent=self._classifier_agent, app_name=APP_NAME, session_service=self._session_service
        )
        self._mapper_runner = Runner(
            agent=self._mapper_agent, app_name=APP_NAME, session_service=self._session_service
        )

    async def classify_input(self, question: str) -> ClassificationResult:
        q = question.lower().strip()
        has_metric, has_dimension, has_qualifier = _classify_query_shape(q)
        signals = ExtractedSignals(
            has_metric=has_metric,
            has_dimension=has_dimension,
            has_qualifier=has_qualifier,
            is_listing=_is_listing_intent(q),
            references_previous=_any_word_in(_REFERENCES_PREVIOUS_PHRASES, q),
        )

        reply = await self._run_single_turn("classifier", f"User message: {question}")
        intent = _parse_classifier_reply(reply)
        return ClassificationResult(intent=intent, confidence=0.85, extracted_signals=signals)

    async def map_terms_to_columns(self, question: str, catalog: SchemaCatalog) -> TermMappingResult:
        # Deterministic, catalog-grounded - see module docstring for why this isn't
        # re-asked of the model.
        filters, consumed_dimensions, compare_dimension = _extract_named_filters(question, catalog)

        reply = await self._run_single_turn("mapper", self._build_mapper_prompt(question))
        metric_intent, dimension_intent = _parse_mapper_reply(reply)

        if dimension_intent in consumed_dimensions:
            dimension_intent = None

        mappings = []
        if metric_intent:
            mappings.append(ColumnMapping(metric_intent, 0.85))
        if dimension_intent:
            mappings.append(ColumnMapping(dimension_intent, 0.85))

        q = question.lower()
        years = _extract_years(q)
        top_n_match = _TOP_N_RE.search(q)

        return TermMappingResult(
            mappings=mappings,
            metric_intent=metric_intent,
            dimension_intent=dimension_intent,
            filters=tuple(filters),
            compare_dimension=compare_dimension,
            year=years[0] if len(years) == 1 else None,
            years=years,
            top_n=int(top_n_match.group(1)) if top_n_match else None,
            all_time=_ALL_TIME_RE.search(q) is not None,
        )

    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        resolved: ResolvedQuery,
        error_feedback: str | None = None,
    ) -> str:
        prompt = self._build_prompt(question, catalog.as_prompt_context(), resolved, error_feedback)
        reply = await self._run_single_turn("generator", prompt)
        return _strip_markdown_fence(reply)

    async def _run_single_turn(self, role: str, prompt: str) -> str:
        """Runs one prompt for `role` ("classifier" | "mapper" | "generator"). On a
        quota/rate-limit error, advances to the next model in the fallback chain and
        retries, so one exhausted free-tier quota doesn't take the feature down."""
        while True:
            runner = getattr(self, _ROLE_RUNNER_ATTR[role])
            try:
                return await self._run_with_runner(runner, prompt)
            except genai_errors.ClientError as exc:
                is_quota = getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(exc)
                if not is_quota or self._model_index + 1 >= len(self._models):
                    raise
                self._model_index += 1
                self._build_agents(self._models[self._model_index])

    async def _run_with_runner(self, runner: Runner, prompt: str) -> str:
        """Each call gets its own session - there's no need to lean on ADK's own
        session/memory machinery, since conversation/resolution state is already
        threaded through the prompt text by the caller. All three runners share
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
    def _build_mapper_prompt(question: str) -> str:
        metric_options = ", ".join(alias for alias, _ in _METRIC_SYNONYMS)
        dimension_options = ", ".join(column for column, _ in _DIMENSION_SYNONYMS)
        return (
            f"User question: {question}\n\n"
            f"Metric options: {metric_options}\n"
            f"Dimension options: {dimension_options}"
        )

    @staticmethod
    def _build_prompt(
        question: str,
        schema_context: str,
        resolved: ResolvedQuery,
        error_feedback: str | None,
    ) -> str:
        lines = [f"Schema:\n{schema_context}", "", f"Question: {question}", ""]

        if resolved.is_listing:
            target = resolved.listing_dimension or "the most relevant column"
            lines.append(f"This is a listing request - return DISTINCT values of: {target}")
        else:
            # Labeled "(semantic label...)" inline, not just relying on the system
            # instruction - the model previously read a bare "Resolved metric:
            # total_revenue" as a literal column name and used it unchanged on Tier 2
            # (mv_sales_analysis), where the real column is total_price.
            lines.append(
                f"Resolved metric (semantic label, look up the real column in Schema "
                f"above): {resolved.metric_alias}"
            )
            group_by_cols = (["sale_year"] if len(resolved.years) >= 2 else []) + (
                [resolved.dimension] if resolved.dimension else []
            ) + ([resolved.extra_dimension] if resolved.extra_dimension else [])
            if resolved.series_limit and resolved.dimension:
                lines.append(
                    f"Only include the top {resolved.series_limit} {resolved.dimension} values by the "
                    f"metric (use a subquery: {resolved.dimension} IN (SELECT ... LIMIT {resolved.series_limit}))."
                )
            if group_by_cols:
                lines.append(f"Group by: {', '.join(group_by_cols)}")
            if resolved.filters:
                filter_desc = "; ".join(f"{f.column} {f.op} {f.values}" for f in resolved.filters)
                lines.append(f"Filters: {filter_desc}")
            if len(resolved.years) >= 2:
                lines.append(
                    f"Comparing across years {', '.join(str(y) for y in resolved.years)} - "
                    f"filter with sale_year IN (...), not sale_year = a single year."
                )
            elif resolved.year is not None:
                lines.append(f"Year filter: {resolved.year}")
            if resolved.top_n:
                lines.append(f"Limit to top {resolved.top_n}")

        if error_feedback:
            lines.append("")
            lines.append(f"The previous attempt for THIS question failed: {error_feedback}")
            lines.append("Correct that specific problem in the new query.")

        return "\n".join(lines)


def _parse_classifier_reply(text: str) -> Intent:
    """Parses the classifier agent's single-word reply. Standalone (not a method) so
    it's unit-testable without a live model call - see tests/test_gemini_provider.py."""
    token = text.strip().upper().strip(".:")
    try:
        return Intent[token]
    except KeyError:
        # Fails open to "try to answer" rather than "refuse to answer" - matches the
        # documented failure-open stance for an unparseable reply.
        return Intent.DATABASE_QUERY


def _parse_mapper_reply(text: str) -> tuple[str | None, str | None]:
    """Parses the mapper agent's METRIC:/DIMENSION: reply, validating each against the
    same closed vocabulary given in the prompt - an out-of-vocabulary or unparseable
    value is treated as NONE, never passed through. Standalone so it's unit-testable
    without a live model call."""
    metric: str | None = None
    dimension: str | None = None
    for line in text.splitlines():
        line = line.strip()
        upper = line.upper()
        if upper.startswith("METRIC:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate in _METRIC_ALIASES:
                metric = candidate
        elif upper.startswith("DIMENSION:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate in _DIMENSION_COLUMNS:
                dimension = candidate
    return metric, dimension


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
