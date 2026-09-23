"""Conversation-scope helpers: what the assistant can't answer, how to reshape a
question into the nearest answerable one, and the copy for clarifying questions and
suggested follow-ups.

Everything here is deterministic and dependency-free so it behaves identically with
or without an LLM configured. The goal (from real usage) is that the assistant never:
  * loops on the same clarifying question,
  * silently answers a different question than the one asked, or
  * shrugs at an out-of-scope question without telling the user what it *can* do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

METRIC_LABELS: dict[str, str] = {
    "total_revenue": "total revenue",
    "avg_revenue": "average order value",
    "total_units": "units sold",
    "transaction_count": "number of transactions",
}

DIMENSION_LABELS: dict[str, str] = {
    "store_division": "division",
    "store_district": "district",
    "item_name": "item",
    "item_supplier": "supplier",
    "item_manufacturer_country": "manufacturer country",
    "payment_type": "payment type",
    "payment_bank": "bank",
    "sale_quarter": "quarter",
    "sale_month": "month",
    "sale_year": "year",
}

TIME_DIMENSIONS: tuple[str, ...] = ("sale_month", "sale_quarter", "sale_year")

# Pairs of non-time dimensions that form a natural hierarchy, so grouping by both at
# once is meaningful ("stores per division and district").
HIERARCHY_PAIRS: frozenset[tuple[str, str]] = frozenset({("store_division", "store_district")})


def metric_label(alias: str | None) -> str:
    return METRIC_LABELS.get(alias or "", (alias or "value").replace("_", " "))


def dimension_label(column: str | None) -> str:
    return DIMENSION_LABELS.get(column or "", (column or "").replace("_", " "))


def _has_word(words: tuple[str, ...], text: str) -> str | None:
    for w in words:
        if re.search(rf"\b{re.escape(w)}s?\b", text):
            return w
    return None


# ---------------------------------------------------------------------------
# Short replies to a clarifying question
# ---------------------------------------------------------------------------

_ALL_TIME_RE = re.compile(
    r"\ball[\s-]*time\b|\boverall\b|\ball (the )?years\b|\bevery year\b|\bwhole period\b"
    r"|\bentire (period|history|dataset)\b|\bno (time |date |year )?filter\b|\bany time\b"
    r"|\blifetime\b|\ball data\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|please|sounds good|"
    r"that works|fine|correct|right|y)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(no|nope|nah|cancel|never ?mind|stop|not now|n)\b", re.IGNORECASE
)
_ACCEPT_DEFAULT_RE = re.compile(
    r"\b(whatever|any(thing)?|you (choose|decide|pick)|default|doesn'?t matter|"
    r"don'?t care|up to you|just the total|total only|only the total)\b",
    re.IGNORECASE,
)


# Data-modification requests. The agent's DB role is read-only and the AST guardrail
# rejects DML anyway - but the user must get an explicit "no", not a report that
# reads as if the request was carried out.
_WRITE_INTENT_RE = re.compile(
    r"\b(delete|remove|drop|truncate|erase|wipe|purge|insert|update|modify|edit|alter|"
    r"overwrite|rename|reset|clear out)\b.{0,60}?\b(records?|rows?|data|tables?|entr(y|ies)|"
    r"sales?|transactions?|values?|columns?|stores?|items?|products?|prices?|revenue|"
    r"database|db|history|everything|all)\b",
    re.IGNORECASE,
)


def is_write_request(text: str) -> bool:
    return _WRITE_INTENT_RE.search(text) is not None


def write_refusal(proposed_question: str | None) -> str:
    text = (
        "I can't do that - I have **read-only** access to the sales data, so I can't delete, "
        "change, or add records (every query I run is verified to be a read-only SELECT). "
        "Changes to the data need to go through your database administrator."
    )
    if proposed_question:
        text += f"\n\nI can show you the data instead - for example **{proposed_question}**. Want me to run that?"
    return text


# "the maximum revenue generating store", "which district had the highest revenue":
# a superlative over a SINGULAR entity asks for exactly one - the top 1.
_SUPERLATIVE_RE = re.compile(
    r"\b(max|maximum|highest|best|largest|biggest|leading|most|top|greatest|strongest)\b",
    re.IGNORECASE,
)


def wants_single_top(text: str, singular_words: tuple[str, ...]) -> bool:
    q = text.lower()
    if not _SUPERLATIVE_RE.search(q):
        return False
    return any(
        re.search(rf"\b{re.escape(w)}\b", q) and not re.search(rf"\b{re.escape(w)}s\b", q)
        for w in singular_words
    )


_CHART_PREF_RE = re.compile(
    r"\b(line|bar|column)\s*(chart|graph|plot)s?\b|\b(as|in|into|with) an? (table|grid)\b|\btable view\b|\btabular\b",
    re.IGNORECASE,
)


def detect_chart_preference(text: str) -> str | None:
    m = _CHART_PREF_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return "line" if m.group(1).lower() == "line" else "bar"
    return "table"


def is_all_time(text: str) -> bool:
    return _ALL_TIME_RE.search(text) is not None


def is_affirmative(text: str) -> bool:
    return _AFFIRMATIVE_RE.search(text) is not None and len(text.split()) <= 6


def is_negative(text: str) -> bool:
    return _NEGATIVE_RE.search(text) is not None and len(text.split()) <= 6


def accepts_default(text: str) -> bool:
    return _ACCEPT_DEFAULT_RE.search(text) is not None


# ---------------------------------------------------------------------------
# Concepts the data genuinely cannot answer -> explain + reshape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnsupportedConcept:
    key: str
    matched_word: str
    explanation: str
    substitute_metric: str


# (key, trigger words, explanation template, closest answerable metric)
_UNSUPPORTED: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "profit",
        ("profit", "margin", "markup", "cost", "cogs", "expense", "loss", "roi"),
        "I don't have cost or expense data, so I can't calculate {word}.",
        "total_revenue",
    ),
    (
        "customer",
        ("customer", "client", "buyer", "shopper", "consumer", "loyalty", "churn"),
        "Customer-level data is deliberately excluded from what I can see (privacy), "
        "so I can't answer questions about individual {word}s.",
        "transaction_count",
    ),
    (
        "staff",
        ("employee", "staff", "salesperson", "salesman", "cashier", "worker", "salary", "wage"),
        "I don't have any staff or employee data.",
        "total_revenue",
    ),
    (
        "inventory",
        ("inventory", "stock level", "in stock", "stockout", "warehouse", "restock"),
        "I don't have inventory or stock-level data - only what was actually sold.",
        "total_units",
    ),
    (
        "forecast",
        ("forecast", "predict", "prediction", "projection", "future", "next year", "next month"),
        "I can only report on historical sales{years}, not forecasts or predictions.",
        "total_revenue",
    ),
    (
        "promotion",
        ("discount", "promotion", "coupon", "campaign", "marketing", "advertising"),
        "I don't have promotion, discount, or marketing data.",
        "avg_revenue",
    ),
    (
        "feedback",
        ("rating", "review", "feedback", "satisfaction", "complaint", "refund"),
        "I don't have ratings, reviews, or refund data.",
        "transaction_count",
    ),
)


def detect_unsupported(question: str, year_range: str | None = None) -> UnsupportedConcept | None:
    q = question.lower()
    for key, words, template, substitute in _UNSUPPORTED:
        word = _has_word(words, q)
        if word:
            years = f" ({year_range})" if year_range else ""
            return UnsupportedConcept(
                key=key,
                matched_word=word,
                explanation=template.format(word=word, years=years),
                substitute_metric=substitute,
            )
    return None


# ---------------------------------------------------------------------------
# Canonical question text (re-parseable by the rule-based mapper)
# ---------------------------------------------------------------------------


def describe_query(
    metric_alias: str | None,
    dimension: str | None = None,
    extra_dimension: str | None = None,
    year: int | None = None,
    years: tuple[int, ...] = (),
    top_n: int | None = None,
    filter_values: tuple[str, ...] = (),
) -> str:
    """e.g. "Total revenue by district and month in 2020" - phrased so that feeding it
    back through MockLLMProvider resolves to the same query."""
    label = metric_label(metric_alias or "total_revenue")
    if top_n == 1 and dimension:
        text = f"Top {dimension_label(dimension)} by {label}"
        if extra_dimension:
            text += f", by {dimension_label(extra_dimension)}"
            extra_dimension = None
    elif top_n and dimension:
        text = f"Top {top_n} {dimension_label(dimension)}s by {label}"
    else:
        text = label[0].upper() + label[1:]
        if dimension:
            text += f" by {dimension_label(dimension)}"
    if extra_dimension:
        text += f" and {dimension_label(extra_dimension)}"
    if filter_values:
        text += " for " + " and ".join(f"'{v}'" for v in filter_values)
    if len(years) >= 2:
        text += " for " + " and ".join(str(y) for y in years)
    elif year is not None:
        text += f" in {year}"
    return text


# ---------------------------------------------------------------------------
# Reply copy
# ---------------------------------------------------------------------------


def example_questions(year_max: int | None) -> list[str]:
    y = year_max or 2021
    return [
        "Total revenue by division",
        f"Monthly revenue in {y}",
        "Top 10 items by revenue",
        "Revenue by payment type",
    ]


def out_of_scope_reply(year_range: str | None) -> str:
    span = f" ({year_range})" if year_range else ""
    return (
        "I can't answer that one - I'm a sales analytics assistant, and I can only "
        f"answer questions about the retail sales data I'm connected to{span}.\n\n"
        "You can ask me about:\n"
        "- **Measures:** revenue, units sold, number of transactions, average order value\n"
        "- **Breakdowns:** division, district, item, supplier, manufacturer country, "
        "payment type, bank\n"
        "- **Time:** by year, quarter or month, or for a specific year\n\n"
        "Try one of the suggestions below."
    )


def breakdown_clarification(metric_alias: str) -> str:
    label = metric_label(metric_alias)
    return (
        f"Happy to look at {label}. How would you like to see it - broken down by "
        "something (division, month, item...), for a specific year, or as a single "
        "all-time total?"
    )


def breakdown_suggestions(year_max: int | None) -> list[str]:
    return ["By division", f"By month in {year_max or 2021}", "Top 10 items", "All-time total"]


def metric_clarification(scope_text: str) -> str:
    scope = f" for {scope_text}" if scope_text else ""
    return (
        f"Sure - which measure should I use{scope}? I can show revenue, units sold, "
        "number of transactions, or average order value."
    )


METRIC_SUGGESTIONS = ["Revenue", "Units sold", "Transactions", "Average order value"]


def reshape_offer(explanation: str, proposed_question: str) -> str:
    return (
        f"{explanation} The closest thing I can show is **{proposed_question}** - "
        "would you like me to run that?"
    )


RESHAPE_SUGGESTIONS = ["Yes, show that", "No thanks"]

NOTHING_PENDING_REPLY = (
    "Sure - what would you like to know? You can ask about revenue, units sold or "
    "transactions, broken down by division, district, item, supplier, payment type or time."
)

DECLINED_REPLY = "No problem. What would you like to know about the sales data instead?"


def followup_suggestions(
    metric_alias: str | None,
    dimension: str | None,
    extra_dimension: str | None,
    year: int | None,
    years: tuple[int, ...],
    top_n: int | None,
    year_max: int | None,
    year_min: int | None = None,
) -> list[str]:
    """Context-aware next questions, phrased as follow-ups that session-state
    resolution understands (they inherit whatever this turn left unspecified)."""
    out: list[str] = []
    if year is None and not years and year_max:
        out.append(f"Only for {year_max}")
    elif year is not None and (year_min is None or year - 1 >= year_min):
        out.append(f"Compare {year - 1} and {year}")
    if dimension not in TIME_DIMENSIONS and extra_dimension is None and dimension is not None:
        out.append("Show it by month")
    if dimension is None:
        out.append("By division")
    if metric_alias == "total_revenue":
        out.append("Show units sold instead")
    elif metric_alias is not None:
        out.append("Show revenue instead")
    if dimension and dimension not in TIME_DIMENSIONS and not top_n and extra_dimension is None:
        out.append(f"Top 5 {dimension_label(dimension)}s")
    return out[:3]
