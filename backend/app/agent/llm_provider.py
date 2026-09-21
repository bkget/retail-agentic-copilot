"""Formal tool-shaped pipeline: classify_input -> map_terms_to_columns -> (merge with
session state, in orchestrator.py) -> generate_sql -> [guardrail/execute live in
orchestrator.py]. Every question is classified and semantically mapped before any SQL
is attempted; a genuinely underspecified question gets a clarifying question back
instead of a guessed default, and a question that references prior context ("same as
before", "those", "by district instead") is resolved against `SessionState`, not by
re-parsing previous SQL text (the old approach - see git history).

MockLLMProvider is a deterministic rule-based implementation of these tools, not an
embedding/LLM matcher. "Semantic mapping" here means a comprehensive, hand-maintained
synonym table covering common paraphrases (revenue/income/turnover/...), not open-ended
NLU - a disclosed, intentional scope boundary (see README's Known Limitations).
GeminiADKProvider implements the same tool shape so a real LLM can eventually back it
with genuine semantic understanding.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from app.schema.catalog import SchemaCatalog

# ============================================================================
# Session / conversation types
# ============================================================================


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    sql: str | None
    row_count: int | None
    # Cached so "explain the above result" can replay it without re-running a query -
    # None for conversational/clarification turns, where there's nothing to cache.
    narrative_text: str | None = None


@dataclass
class SessionState:
    """What the agent currently believes the conversation is "about" - distinct from
    raw turn history. This is what lets "same as before but by district" resolve: the
    metric is inherited from state, the dimension comes from the new message."""

    last_metric_alias: str | None = None  # e.g. "total_revenue", "avg_revenue"
    last_dimension: str | None = None  # e.g. "store_division"
    last_year: int | None = None  # time-range slot, tracked separately from active_filters
    active_filters: dict[str, "_NamedFilter"] = field(default_factory=dict)  # column -> named filter
    output_preference: str = "grouped"  # "grouped" | "table"


# SessionContext (history + state bundle) lives in app.session.store, not here - it's
# a session-store return type, not something any function in this module constructs or
# consumes.

# ============================================================================
# classify_input
# ============================================================================


class Intent(str, Enum):
    GREETING = "greeting"
    DATABASE_QUERY = "database_query"
    SCHEMA_INFO = "schema_info"
    CLARIFICATION = "clarification"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExtractedSignals:
    has_metric: bool
    has_dimension: bool
    has_qualifier: bool
    is_listing: bool
    # "same as before" / "those" / "previous" / "last time" / "as above" - a textual
    # signal only; classify_input never sees session state (matches the tool's exact
    # signature), so *resolving* the reference happens later, against SessionState.
    references_previous: bool


@dataclass(frozen=True)
class ClassificationResult:
    intent: Intent
    confidence: float
    extracted_signals: ExtractedSignals


# ============================================================================
# map_terms_to_columns
# ============================================================================


@dataclass(frozen=True)
class ColumnMapping:
    column: str
    confidence: float


@dataclass(frozen=True)
class TermMappingResult:
    mappings: list[ColumnMapping]
    metric_intent: str | None  # resolved metric alias, e.g. "total_revenue" - None if no metric mentioned
    dimension_intent: str | None  # resolved dimension column - None if no dimension mentioned
    filters: tuple["_NamedFilter", ...]
    compare_dimension: str | None
    year: int | None  # the single year, when exactly one was mentioned - None otherwise
    # All distinct years mentioned, sorted ascending - 2+ elements means "compare
    # across these years" (e.g. "revenue for 2019 and 2020"), mirroring how
    # compare_dimension already works for 2+ named values on the same dimension.
    # `year` above is just `years[0]` when len(years) == 1, kept as its own field so
    # every existing single-year call site is unaffected.
    years: tuple[int, ...]
    top_n: int | None


# ============================================================================
# Resolved query (post session-state merge, ready for SQL assembly)
# ============================================================================


@dataclass(frozen=True)
class ResolvedQuery:
    metric_alias: str | None = None
    dimension: str | None = None
    filters: tuple["_NamedFilter", ...] = ()
    year: int | None = None
    # 2+ elements when the question compares across multiple years - takes priority
    # over `year` in SQL generation (sale_year IN (...) instead of sale_year = ...),
    # and sale_year is added as its own GROUP BY column alongside `dimension` (not
    # instead of it) when set, so "revenue by quarter for 2019 and 2020" groups by
    # both sale_year and sale_quarter, not just one or the other.
    years: tuple[int, ...] = ()
    top_n: int | None = None
    is_listing: bool = False
    listing_dimension: str | None = None


def resolve_with_session(
    mapping: TermMappingResult, signals: ExtractedSignals, state: SessionState
) -> ResolvedQuery:
    """Slot-by-slot merge of this turn's explicit mapping with prior session state.

    Whatever the message explicitly mentions always wins (and is what the caller
    should write back to state for the *next* turn via update_session); a slot the
    message left silent is inherited from state only when the message actually points
    at prior context (`references_previous` - "same as before", "those", "explain
    that") or is shaped like a bare continuation of the same topic (mentions a
    dimension/filter/year of its own but no metric - "by district instead", "only for
    2024"). A message with an explicit metric AND an explicit dimension is a fresh,
    standalone query - nothing is inherited even if references_previous also happens
    to be set, since it doesn't need anything from state to be complete.

    Deliberately does not decide "is this worth answering at all" - a bare, no-signal
    fragment like "revenue" never reaches this function, because classify_input routes
    it straight to CLARIFICATION before any mapping is attempted (see Orchestrator).
    By the time this runs, the caller has already decided the message is either a real
    query or a real reference to prior context.
    """
    if signals.is_listing:
        return ResolvedQuery(is_listing=True, listing_dimension=mapping.dimension_intent)

    metric_alias = mapping.metric_intent
    dimension = mapping.compare_dimension or mapping.dimension_intent
    year = mapping.year
    # 2+ years mentioned in THIS message is unambiguous and self-contained - never
    # inherited from state (there's nothing to inherit; it's already complete).
    years = mapping.years if len(mapping.years) >= 2 else ()
    filters_by_column: dict[str, _NamedFilter] = {f.column: f for f in mapping.filters}

    is_fresh_standalone = metric_alias is not None and dimension is not None
    is_continuation = metric_alias is None and (
        dimension is not None or filters_by_column or year is not None or years or signals.has_qualifier
    )
    if not is_fresh_standalone and (signals.references_previous or is_continuation):
        if metric_alias is None:
            metric_alias = state.last_metric_alias
        if dimension is None:
            dimension = state.last_dimension
        if year is None and not years:
            year = state.last_year
        for column, named_filter in state.active_filters.items():
            filters_by_column.setdefault(column, named_filter)

    return ResolvedQuery(
        metric_alias=metric_alias,
        dimension=dimension,
        filters=tuple(filters_by_column.values()),
        year=year,
        years=years,
        top_n=mapping.top_n,
    )


class LLMProvider(ABC):
    @abstractmethod
    async def classify_input(self, question: str) -> ClassificationResult:
        """Decides what kind of message this is before any mapping/SQL is attempted -
        greetings and schema/capability questions never reach mapping, and a question
        with no recognizable signal gets routed to clarification/unknown instead of a
        guessed default. Deliberately stateless (no session access, matching the exact
        tool signature) - reference resolution against session state happens after
        this, in the orchestrator, which does have session access."""

    @abstractmethod
    async def map_terms_to_columns(self, question: str, catalog: SchemaCatalog) -> TermMappingResult:
        """Resolves the question's words into real schema columns - a metric, an
        optional group-by dimension, and any named-value filters - via synonym
        matching, not by guessing column names. Returns None for any slot the question
        doesn't mention at all, so the caller can tell "not mentioned" (inheritable
        from session state) apart from "mentioned but unmappable" (needs clarification).

        Takes the full `catalog` rather than a flat column-name list: named-value
        filtering ("cash", "'Dhaka'") needs the real enum *values* loaded from the
        database (`catalog.enum_hints`), not just column existence."""

    @abstractmethod
    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        resolved: ResolvedQuery,
        error_feedback: str | None = None,
    ) -> str:
        """Builds SQL from an already-resolved query (post session-state merge, see
        orchestrator.py::resolve_with_session). Never trusted as-is - always passed
        through app.security.ast_guardrail before execution. `error_feedback`, when
        set, is the guardrail/DB error from a previous failed attempt in the same
        turn, for self-correction."""


class LLMGenerationError(RuntimeError):
    pass


# ============================================================================
# Low-level shared helpers
# ============================================================================

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)


def _extract_years(text: str) -> tuple[int, ...]:
    """All distinct years mentioned, sorted ascending - not just the first. Shared by
    both providers' map_terms_to_columns. Regression note: "compare revenue for 2019
    and 2020" used to silently resolve to 2019 only (a bare `.search()` call only ever
    finds the first match), which looked like a working answer but quietly dropped
    half the question - the exact "confidently wrong instead of asking/handling it"
    failure mode this whole architecture exists to prevent."""
    return tuple(sorted({int(m.group()) for m in _YEAR_RE.finditer(text)}))


def _word_in(keyword: str, text: str) -> bool:
    """Whole-word match (allowing a trailing "s" for regular plurals), not substring -
    plain `in` checks are exactly how "country" (contains "count") got routed to
    COUNT(*) and "average order value" (contains "order") got routed to a transaction
    count instead of AVG. Every keyword lookup in this router goes through this, not
    `in`, for that reason. Case-sensitive by design - callers are expected to lowercase
    the question once up front and keep every table entry lowercase, rather than pay
    for `re.IGNORECASE` on every single lookup."""
    return re.search(rf"\b{re.escape(keyword)}s?\b", text) is not None


def _any_word_in(words: tuple[str, ...], text: str) -> bool:
    return any(_word_in(w, text) for w in words)


# ============================================================================
# Synonym tables - the "semantic mapping" dictionary. Comprehensive and hand
# maintained (see module docstring for why this isn't ML-semantic), organized so a new
# synonym is a one-line addition. The first word in each tuple is treated as the
# canonical term (confidence 1.0); every other entry is a synonym (confidence 0.85).
# ============================================================================

# Ordered by priority, most specific first: a question mentioning both "payment" and
# "bank" (e.g. "revenue by payment bank") should resolve to the more specific
# payment_bank column, not the generic payment_type one - so "bank" must be checked
# before "payment". Order matters throughout this list, not just for that pair.
_DIMENSION_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("payment_bank", ("bank",)),
    ("store_division", ("division", "department", "business unit", "segment", "region")),
    ("store_district", ("district", "area", "locality")),
    ("item_supplier", ("supplier", "vendor", "wholesaler", "source")),
    (
        "item_manufacturer_country",
        (
            "country", "countries", "manufacturer country", "country of origin",
            "manufacturing origin", "producer country",
        ),
    ),
    ("item_name", ("item", "product", "sku", "article")),
    ("payment_type", ("payment", "payment method", "payment channel", "tender type")),
    ("sale_quarter", ("quarter",)),
    ("sale_month", ("month",)),
    ("sale_year", ("year",)),
]

# "average"/"avg" checked first and deliberately: it must win over a coincidental
# whole-word match like "order" inside "average order value" - table order here is
# real precedence, not just a list of options. "revenue" is intentionally its own,
# highest-priority non-average entry: it's unambiguous, unlike "sales" (which could
# mean revenue OR volume, e.g. "total sales volume") - so only "revenue" and its
# synonyms get to override an incidental unit/transaction word elsewhere in the same
# sentence (see eval/README.md's "total revenue... for cash transactions" case).
_METRIC_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("avg_revenue", ("average", "avg")),
    (
        "total_revenue",
        ("revenue", "income", "turnover", "earnings", "net revenue", "gross revenue", "sales amount"),
    ),
    (
        "total_units",
        ("unit", "units sold", "quantity sold", "sold units", "volume sold", "quantity", "volume", "sold"),
    ),
    (
        "transaction_count",
        ("transaction", "order", "sales transactions", "trade record", "deal", "how many", "count"),
    ),
]

# Every synonym word across the metric table, plus a couple of generic/ambiguous words
# ("sales", "price", "value") that signal *a* metric is being asked about without
# pinning down which one - used to answer "does this question mention a metric at
# all", independent of which specific one map_terms_to_columns later resolves.
_METRIC_SIGNAL_WORDS: tuple[str, ...] = tuple(
    word for _, words in _METRIC_SYNONYMS for word in words
) + ("sales", "price", "value")

# Columns only present on the row-level Tier 2 view (mv_sales_daily_rollup's grain is
# sale_date/sale_year/sale_month x store_division/store_district x item_name only - no
# payment or quarter columns). Their presence forces Tier 2 even if a
# Tier-1-satisfiable dimension (division/month/...) is also involved.
_TIER2_ONLY_DIMENSIONS = {
    "item_supplier", "item_manufacturer_country", "payment_bank",
    "payment_type", "sale_quarter",
}

# --- Intent classification word lists -----------------------------------------------

_GREETING_WORDS = (
    "hi", "hii", "hello", "hey", "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "bye", "goodbye",
)

# Covers both "what can I ask" (capabilities) and "what years/data do you have"
# (coverage) - both are SCHEMA_INFO per the routing spec: questions about the data or
# the assistant itself, not an analytical question. `_DATA_COVERAGE_PHRASES` is the
# subset used later (in orchestrator.py) to pick which of the two reply texts to show.
_DATA_COVERAGE_PHRASES = (
    "data you have access", "years of data", "date range", "which years",
    "what years", "years available", "time period do you have",
)
_CAPABILITY_PHRASES = (
    "what can you", "what can i ask", "what kind of questions", "what columns",
    "tell me about yourself", "help",
    "what kind of data", "kind of data", "type of data", "what data do you have",
    # Bare, highly domain-specific words: "columns"/"questions" essentially never occur
    # in a genuine sales question, but do in real phrasings of "what can I ask" that
    # don't happen to match the literal multi-word phrases above verbatim - e.g. "what
    # ARE THE columns..." doesn't contain the literal substring "what columns".
    "columns", "questions",
)
_SCHEMA_INFO_PHRASES = _DATA_COVERAGE_PHRASES + _CAPABILITY_PHRASES


def is_data_coverage_question(question: str) -> bool:
    return _any_word_in(_DATA_COVERAGE_PHRASES, question.lower())


# A reference to prior context ("explain the above result in plain english", "same as
# before", "those", "by district instead") - resolved against SessionState by the
# orchestrator, not here (classify_input has no session access).
_REFERENCES_PREVIOUS_PHRASES = (
    "explain", "in plain english", "summarize", "what does that mean",
    "what does this mean", "elaborate", "same as before", "same as", "those",
    "that", "previous", "last time", "as above", "the above", "instead",
)

# Any one of these (plus a year, "top N", or a recognized dimension synonym) turns a
# metric mention into a complete, answerable question - "total revenue" and "how many
# transactions were there?" both qualify; bare "revenue" / "show me revenue" do not.
_QUALIFYING_WORDS = ("total", "average", "avg", "by", "how many", "all")

# "Available"/"list" without a ranking word ("top N") signals "show me the distinct
# values", not "rank these by a metric".
_LISTING_TRIGGER_WORDS = ("available",)

GREETING_REPLY = (
    "Hi! \U0001F44B How can I help you today? You can ask me about revenue, sales, "
    "products, stores, payments, suppliers, or business trends."
)

# Deliberately does NOT mention customers: the semantic layer excludes customer data
# entirely (see db/README.md - PII exclusion), so claiming it here would set the user
# up for a query that can never succeed.
SCHEMA_INFO_REPLY = (
    "You can ask me about:\n"
    "- Revenue and sales\n"
    "- Units sold and transaction counts\n"
    "- Stores, divisions, and districts\n"
    "- Products and suppliers\n"
    "- Manufacturer countries\n"
    "- Payment types and banks\n"
    "- Time-based trends (by year, quarter, or month)\n"
    "- Top-performing items, suppliers, or divisions\n\n"
    'For example: "total revenue by division", "top 10 items by revenue", '
    '"average order value", "revenue by quarter in 2020".'
)

CLARIFYING_REPLY_TIME_PERIOD = (
    "Sure - which time period would you like to look at? For example, a specific "
    "year (e.g. 2020), or all-time?"
)

# For questions with no recognizable metric, dimension, year, or "top N" at all -
# nothing in the schema to anchor a query to, and no session state to inherit from
# either. Deliberately states the actual scope rather than guessing: silently
# defaulting to a bare revenue total for an unrelated question is exactly the
# "lacking interaction" failure mode this replaces.
UNKNOWN_REPLY = (
    "I couldn't find a way to answer that from the sales data I have - revenue, units "
    "sold, transactions, by division, district, item, supplier, manufacturer country, "
    "payment type or bank, over time. Could you rephrase, or ask about one of those?"
)


def _classify_query_shape(q: str) -> tuple[bool, bool, bool]:
    """Returns (has_metric, has_dimension, has_qualifier). A dimension synonym or a
    qualifier (total/average/"by"/a year/"top N") is, on its own, enough signal to
    treat the question as answerable - a metric word with neither is what makes a
    question a fragment ("revenue") rather than a real question ("total revenue")."""
    has_metric = _any_word_in(_METRIC_SIGNAL_WORDS, q)
    has_dimension = any(_any_word_in(words, q) for _, words in _DIMENSION_SYNONYMS)
    has_qualifier = (
        _any_word_in(_QUALIFYING_WORDS, q)
        or _YEAR_RE.search(q) is not None
        or _TOP_N_RE.search(q) is not None
    )
    return has_metric, has_dimension, has_qualifier


def _is_listing_intent(q: str) -> bool:
    """"What are the available X" / "list the X" - wants distinct values, not a metric
    ranking. A metric word rules this out entirely regardless of the rest: "List total
    revenue... broken down by district" is a breakdown request ("list" used as a
    synonym for "show me"), not an enumeration of distinct district names. A bare
    "list " prefix alone is also not enough on its own - it must pair with an actual
    recognized dimension synonym, or it risks misreading an unrelated, unanswerable
    question ("List the name of the sales in the company") as a listing request."""
    has_metric, _, _ = _classify_query_shape(q)
    if has_metric:
        return False
    if _TOP_N_RE.search(q) or _word_in("top", q):
        return False
    if _any_word_in(_LISTING_TRIGGER_WORDS, q):
        return True
    if q.strip().startswith("list "):
        return any(_any_word_in(words, q) for _, words in _DIMENSION_SYNONYMS)
    return False


# "'Dhaka'", "'Pepsi - 12 oz cans'", or double-quoted equivalents - a quoted phrase in
# the question is a strong signal the user named a specific value, not a dimension to
# group by.
_QUOTED_VALUE_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")


def _extract_quoted_values(question: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _QUOTED_VALUE_RE.finditer(question)]


def _sql_escape(value: str) -> str:
    """Defense in depth beyond the AST guardrail: this is direct string interpolation
    of question-derived (user-influenced) text into SQL text, so it must be escaped at
    construction time, not just relied on to be caught downstream."""
    return value.replace("'", "''")


@dataclass(frozen=True)
class _NamedFilter:
    column: str
    op: str  # "=" | "IN" | "ILIKE"
    values: tuple[str, ...]  # one value for "="/"ILIKE" with a single match, more for "IN"/multi-ILIKE

    def to_sql(self) -> str:
        if self.op == "=":
            return f"{self.column} = '{_sql_escape(self.values[0])}'"
        if self.op == "IN":
            joined = ", ".join(f"'{_sql_escape(v)}'" for v in self.values)
            return f"{self.column} IN ({joined})"
        if self.op == "ILIKE":
            if len(self.values) == 1:
                return f"{self.column} ILIKE '%{_sql_escape(self.values[0])}%'"
            clauses = " OR ".join(f"{self.column} ILIKE '%{_sql_escape(v)}%'" for v in self.values)
            return f"({clauses})"
        raise ValueError(f"unknown filter op {self.op!r}")  # pragma: no cover - internal invariant


def _extract_named_filters(
    question: str, catalog: SchemaCatalog
) -> tuple[list[_NamedFilter], set[str], str | None]:
    """Resolves named entities in the question ("'Dhaka'", "cash", "'China'") into
    WHERE filters against the real enum values loaded from the database
    (catalog.enum_hints) - not a hardcoded list, so it can't drift from the real data
    the way the original spec's hardcoded hints did (see db/README.md).

    Matching is case-insensitive and works whether the value is quoted ("'Dhaka'") or
    bare ("cash transactions") - `_word_in` against each known enum value, lowercased.
    The *canonical* (as-stored) value is what goes into the generated SQL, so this also
    transparently handles the known man_country casing quirk ("poland" stored
    lowercase): asking about "'Poland'" still filters on the value that's actually in
    the database.

    Returns (filters, consumed_dimensions, compare_dimension):
      - consumed_dimensions: dimension columns resolved as a *filter* value, not a
        group-by request - "for the 'Dhaka' division" filters to Dhaka, it doesn't ask
        to break down by division, so `store_division` must not also be picked as the
        GROUP BY target by the normal synonym scan.
      - compare_dimension: set when 2+ distinct values matched the same dimension
        ("compare 'Dhaka' and 'Chittagong'") - that dimension is forced as the GROUP BY
        target instead of being excluded, since a compare request specifically wants
        one row per named entity.
    """
    q = question.lower()
    filters: list[_NamedFilter] = []
    consumed_dimensions: set[str] = set()
    compare_dimension: str | None = None
    matched_lower: set[str] = set()

    for column, values in catalog.enum_hints.items():
        matches = [v for v in values if v and _word_in(v.lower(), q)]
        if not matches:
            continue
        matched_lower.update(v.lower() for v in matches)
        if len(matches) >= 2:
            filters.append(_NamedFilter(column, "IN", tuple(matches)))
            compare_dimension = column
        else:
            filters.append(_NamedFilter(column, "=", (matches[0],)))
        consumed_dimensions.add(column)

    # Quoted phrases not already resolved as a known enum value are treated as
    # item_name references (free text, not enum-hinted - exact match is too brittle
    # for product names, hence ILIKE rather than "=").
    unmatched_quoted = [
        v for v in _extract_quoted_values(question) if v.lower() not in matched_lower
    ]
    if unmatched_quoted:
        filters.append(_NamedFilter("item_name", "ILIKE", tuple(unmatched_quoted)))
        if len(unmatched_quoted) >= 2 and compare_dimension is None:
            compare_dimension = "item_name"

    return filters, consumed_dimensions, compare_dimension


def _metric_sql_expr(metric_alias: str, use_tier2: bool) -> str:
    exprs = {
        "avg_revenue": "AVG(total_price)" if use_tier2 else "AVG(total_revenue)",
        "total_revenue": "SUM(total_price)" if use_tier2 else "SUM(total_revenue)",
        "total_units": "SUM(quantity)" if use_tier2 else "SUM(total_units_sold)",
        "transaction_count": "COUNT(*)" if use_tier2 else "SUM(transaction_count)",
    }
    return exprs.get(metric_alias, exprs["total_revenue"])


class MockLLMProvider(LLMProvider):
    """Deterministic rule-based implementation of the classify_input /
    map_terms_to_columns / generate_sql tool pipeline. No network calls, no
    randomness - same question always produces the same result, which is what makes
    it usable for eval harness ground-truth comparisons.
    """

    async def classify_input(self, question: str) -> ClassificationResult:
        q = question.lower().strip()

        if _any_word_in(_GREETING_WORDS, q):
            return ClassificationResult(
                Intent.GREETING, 1.0, ExtractedSignals(False, False, False, False, False)
            )

        if _any_word_in(_SCHEMA_INFO_PHRASES, q):
            return ClassificationResult(
                Intent.SCHEMA_INFO, 1.0, ExtractedSignals(False, False, False, False, False)
            )

        references_previous = _any_word_in(_REFERENCES_PREVIOUS_PHRASES, q)
        is_listing = _is_listing_intent(q)
        has_metric, has_dimension, has_qualifier = _classify_query_shape(q)
        signals = ExtractedSignals(has_metric, has_dimension, has_qualifier, is_listing, references_previous)

        if is_listing:
            return ClassificationResult(Intent.DATABASE_QUERY, 0.9, signals)

        # A qualifier alone (e.g. just a year) is enough - this is also what keeps a
        # bare-filter follow-up ("Only for 2024") routed to DATABASE_QUERY, since it
        # has neither a metric nor a dimension synonym, only the year.
        #
        # references_previous is checked in this same first branch, not after the bare
        # has_metric check below - a message like "same as before but revenue instead"
        # DOES have a metric word, so checking has_metric first would send it to plain
        # CLARIFICATION and never give session-state resolution a chance. Whether that
        # reference actually resolves to anything is decided later, against real
        # SessionState (the orchestrator falls back to UNKNOWN/clarification itself if
        # state has nothing to offer) - classify_input has no session access and can
        # only act on the textual signal.
        if has_dimension or has_qualifier or references_previous:
            return ClassificationResult(Intent.DATABASE_QUERY, 0.9 if not references_previous else 0.7, signals)

        # No dimension/qualifier/reference signal, but a bare metric word ("revenue",
        # "show me units sold") - a fragment, not a complete question. Deliberately
        # NOT resolved via session-state inheritance even if state has a prior metric:
        # silently answering a topic-less "revenue" with an old dimension/filter is the
        # "confidently wrong instead of asking" failure mode this taxonomy exists to
        # avoid - so this always asks, never inherits.
        if has_metric:
            return ClassificationResult(Intent.CLARIFICATION, 0.75, signals)

        return ClassificationResult(Intent.UNKNOWN, 0.5, signals)

    async def map_terms_to_columns(self, question: str, catalog: SchemaCatalog) -> TermMappingResult:
        q = question.lower()

        mappings: list[ColumnMapping] = []

        # Extracted first, not after the dimension scan: a dimension keyword resolved
        # purely as a filter value's descriptor ("for the 'Dhaka' division") isn't also
        # a group-by request, and the dimension scan below needs to know that *while*
        # picking a target, not null one out afterward - nulling out after would throw
        # away the pick entirely instead of falling through to the next candidate (e.g.
        # "by month for the 'Dhaka' division" must still resolve to sale_month, not
        # silently end up with no dimension at all just because "division" also
        # happens to appear in the same sentence).
        filters, consumed_dimensions, compare_dimension = _extract_named_filters(question, catalog)

        dimension_intent: str | None = None
        for column, words in _DIMENSION_SYNONYMS:
            for word in words:
                if _word_in(word, q):
                    mappings.append(ColumnMapping(column, 1.0 if word == words[0] else 0.85))
                    if dimension_intent is None and column not in consumed_dimensions:
                        dimension_intent = column
                    break

        metric_intent: str | None = None
        for alias, words in _METRIC_SYNONYMS:
            for word in words:
                if _word_in(word, q):
                    mappings.append(ColumnMapping(alias, 1.0 if word == words[0] else 0.85))
                    if metric_intent is None:
                        metric_intent = alias
                    break
            if metric_intent:
                break

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
        )

    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        resolved: ResolvedQuery,
        error_feedback: str | None = None,
    ) -> str:
        if resolved.is_listing:
            dimension = resolved.listing_dimension or "item_name"
            use_tier2 = dimension in _TIER2_ONLY_DIMENSIONS
            table = "public.mv_sales_analysis" if use_tier2 else "public.mv_sales_daily_rollup"
            return f"SELECT DISTINCT {dimension} FROM {table} ORDER BY {dimension} LIMIT 500"

        metric_alias = resolved.metric_alias or "total_revenue"
        use_tier2 = (
            metric_alias == "avg_revenue"
            or resolved.dimension in _TIER2_ONLY_DIMENSIONS
            or any(f.column in _TIER2_ONLY_DIMENSIONS for f in resolved.filters)
        )
        table = "public.mv_sales_analysis" if use_tier2 else "public.mv_sales_daily_rollup"
        metric_expr = _metric_sql_expr(metric_alias, use_tier2)

        where_parts = [f.to_sql() for f in resolved.filters]
        # resolved.year / resolved.years are already ints by this point (parsed via
        # _extract_years upstream), not raw question text - nothing to escape.
        if len(resolved.years) >= 2:
            where_parts.append(f"sale_year IN ({', '.join(str(y) for y in resolved.years)})")
        elif resolved.year is not None:
            where_parts.append(f"sale_year = {resolved.year}")
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        # A multi-year comparison adds sale_year as its own GROUP BY column alongside
        # any other explicit dimension (not instead of it) - "revenue by quarter for
        # 2019 and 2020" groups by both sale_year and sale_quarter, matching what a
        # real analyst (and, independently, Gemini reading the raw question) would do.
        group_by_cols = (["sale_year"] if len(resolved.years) >= 2 else []) + (
            [resolved.dimension] if resolved.dimension else []
        )

        if group_by_cols:
            select_cols = f"{', '.join(group_by_cols)}, {metric_expr} AS {metric_alias}"
            group_by = f"GROUP BY {', '.join(group_by_cols)}"
            # Ordered chronologically for a multi-year comparison (see the trend across
            # time), ranked by value otherwise (unchanged single-dimension behavior).
            order_by = (
                f"ORDER BY {', '.join(group_by_cols)}"
                if len(resolved.years) >= 2
                else f"ORDER BY {metric_alias} DESC"
            )
        else:
            select_cols = f"{metric_expr} AS {metric_alias}"
            group_by = ""
            order_by = ""

        limit_clause = f"LIMIT {resolved.top_n}" if resolved.top_n else ""

        parts = [f"SELECT {select_cols}", f"FROM {table}"]
        if where_clause:
            parts.append(where_clause)
        if group_by:
            parts.append(group_by)
        if order_by:
            parts.append(order_by)
        if limit_clause:
            parts.append(limit_clause)
        return " ".join(parts)
