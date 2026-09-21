"""Deterministic narrative generation. All numbers quoted in the narrative are read
directly from the SQL result set - never re-computed or paraphrased by an LLM - so the
narrative can never say a number that isn't literally in the data the guardrailed query
returned. This is the mechanism behind the spec's "no math hallucinations" requirement.
Any aggregate stated here (a total, a range, a year-over-year delta) is real arithmetic
computed in this module from `rows`, not an LLM's estimate.

Works generically off the shape of the result set (which columns are numeric vs.
categorical) rather than needing structured intent from the SQL-generation step, so it
works the same whether SQL came from MockLLMProvider or a real LLM. `resolved` (the
already-resolved query) is only consulted for scope context (filters/years/top-N) that
isn't otherwise recoverable from the result set alone - e.g. a WHERE-only filter that
narrowed the data but left no trace in which columns came back.

Deliberately prose, not a bulleted "Metric/Breakdown/Filters" preamble - that format
read as a raw dump of internal state rather than an answer. The same information (what
was measured, how it was broken down, what it was filtered to) is woven into the
opening sentence instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Number

from app.agent.llm_provider import ResolvedQuery

CURRENCY = "$"

# Human-friendly phrasing for known metric aliases (see app/agent/llm_provider.py's
# metric_alias values) - falls back to a title-cased column name for anything else, so
# an unrecognized alias still reads reasonably rather than erroring.
_METRIC_LABELS = {
    "total_revenue": "total revenue",
    "avg_revenue": "average order value",
    "total_units": "total units sold",
    "transaction_count": "total number of transactions",
}


def _metric_label(column: str) -> str:
    return _METRIC_LABELS.get(column, column.replace("_", " "))

# Integer-typed but semantically dimensions, not measures - grouping/charting by these
# must never be mistaken for the numeric metric being summarized.
_DIMENSION_LIKE_NUMERIC_COLUMNS = {"sale_year", "sale_month", "sale_day"}

_CURRENCY_METRIC_HINTS = ("revenue", "price", "value", "sales")


def _is_currency_metric(column: str) -> bool:
    """Only money-denominated metrics get a currency label. Without this, a count
    metric (transaction_count, total_units) that happens to cross the 1,000 scaling
    threshold would get "BDT" appended to a number that was never a currency amount."""
    return any(hint in column for hint in _CURRENCY_METRIC_HINTS)


@dataclass(frozen=True)
class NarrativeResult:
    text: str
    formatting: dict


def _is_numeric(column: str, value: object) -> bool:
    if column in _DIMENSION_LIKE_NUMERIC_COLUMNS:
        return False
    return isinstance(value, Number) and not isinstance(value, bool)


def _scale(value: float) -> tuple[float, str, int]:
    """Returns (scaled_value, unit_label, decimals) for readable display. Threshold for
    "millions" is deliberately above the typical single-division revenue range in this
    dataset (~1-2M) so those numbers render as "1,420K", matching the spec's own example,
    rather than jumping to "1.42M" a tier too early."""
    magnitude = abs(value)
    if magnitude >= 10_000_000:
        return value / 1_000_000, "millions", 2
    if magnitude >= 1_000:
        return value / 1_000, "thousands", 0
    return value, "units", 2


_UNIT_SUFFIX = {"thousands": "K", "millions": "M", "units": ""}


def _format_value(value: float, unit: str, decimals: int) -> str:
    return f"{value:,.{decimals}f}{_UNIT_SUFFIX[unit]}"


def _describe_scope(resolved: ResolvedQuery) -> str:
    """A natural trailing clause describing the filters/time range in play - e.g.
    " for Dhaka" or " for 2019 and 2020" - the prose replacement for the old separate
    "Filters: ..." bullet line. Empty string (not a dangling " for") when there's
    nothing to describe."""
    parts: list[str] = [v for f in resolved.filters for v in f.values]
    if len(resolved.years) >= 2:
        parts.append(" and ".join(str(y) for y in resolved.years))
    elif resolved.year is not None:
        parts.append(str(resolved.year))
    if not parts:
        return ""
    return " for " + ", ".join(parts)


def build_narrative(resolved: ResolvedQuery, columns: list[str], rows: list[dict]) -> NarrativeResult:
    if not rows:
        return NarrativeResult(
            text="No rows matched this query.",
            formatting={"currency": CURRENCY, "unit": "units", "decimals": 0},
        )

    numeric_cols = [c for c in columns if _is_numeric(c, rows[0].get(c))]
    categorical_cols = [c for c in columns if c not in numeric_cols]

    if not numeric_cols:
        # A pure "list the distinct X" result (see MockLLMProvider._build_listing_sql) -
        # one categorical column, nothing to aggregate. Show the actual values instead
        # of a content-free "no numeric measures" message.
        if len(categorical_cols) == 1 and rows:
            dim_col = categorical_cols[0]
            dim_label = dim_col.replace("_", " ")
            values = [str(r[dim_col]) for r in rows if r.get(dim_col) is not None]
            sample = values[:8]
            text = f"There are {len(values)} distinct {dim_label} values"
            if sample:
                text += ": " + ", ".join(sample)
                remaining = len(values) - len(sample)
                if remaining > 0:
                    text += f", and {remaining} more"
            text += "."
            return NarrativeResult(
                text=text, formatting={"currency": CURRENCY, "unit": "units", "decimals": 0}
            )
        return NarrativeResult(
            text=f"Returned {len(rows)} row(s) with no numeric measures to summarize.",
            formatting={"currency": CURRENCY, "unit": "units", "decimals": 0},
        )

    metric_col = numeric_cols[-1]
    is_currency = _is_currency_metric(metric_col)
    values = [float(r[metric_col]) for r in rows if r.get(metric_col) is not None]
    representative = max((abs(v) for v in values), default=0.0)
    _, unit, decimals = _scale(representative)
    formatting = {"currency": CURRENCY, "unit": unit, "decimals": decimals}

    def fmt(v: float) -> str:
        scaled = v / {"millions": 1_000_000, "thousands": 1_000, "units": 1}[unit]
        return _format_value(scaled, unit, decimals)

    currency_prefix = CURRENCY if is_currency else ""
    label = _metric_label(metric_col)
    scope = _describe_scope(resolved)

    if not categorical_cols or len(rows) == 1:
        total = sum(values)
        text = f"The {label}{scope} is {currency_prefix}{fmt(total)}."
        return NarrativeResult(text=text, formatting=formatting)

    # Handles 1+ categorical columns uniformly - a single column (the common case)
    # degenerates to exactly the old single-dimension wording; 2+ columns (e.g. a
    # multi-year comparison also broken down by quarter) get a combined label like
    # "2020 Q4" instead of silently describing only the first column and ignoring the
    # rest, which is what used to happen when categorical_cols[0] was the only column
    # ever consulted.
    dim_label = " and ".join(c.replace("_", " ") for c in categorical_cols)

    def combo_label(row: dict) -> str:
        return " ".join(str(row[c]) for c in categorical_cols)

    ranked = sorted(rows, key=lambda r: (r.get(metric_col) or 0), reverse=True)
    n = len(ranked)
    leader = ranked[0]

    top_n_phrase = f"the top {resolved.top_n} " if resolved.top_n else ""
    sentences = [
        f"Looking at {label} by {dim_label}{scope}, {top_n_phrase}results show "
        f"**{combo_label(leader)}** leading with {currency_prefix}{fmt(leader[metric_col])}"
        + (
            f", followed by **{combo_label(ranked[1])}** at "
            f"{currency_prefix}{fmt(ranked[1][metric_col])}"
            if n > 1
            else ""
        )
        + "."
    ]

    # Extra context beyond the top 2 - a total and a range - only once there are
    # enough rows for "the top 2" to have left something out worth mentioning.
    if n >= 3:
        lowest = ranked[-1]
        total = sum(values)
        sentences.append(
            f"Across all {n} results, {label} totals {currency_prefix}{fmt(total)}, "
            f"ranging from {currency_prefix}{fmt(leader[metric_col])} down to "
            f"{currency_prefix}{fmt(lowest[metric_col])} for **{combo_label(lowest)}**."
        )

    # Year-over-year note when sale_year is one of the compared dimensions and the
    # result isn't a top-N slice (a top-N sum wouldn't represent true year totals, so
    # it's deliberately skipped in that case rather than stating a misleading figure).
    if "sale_year" in categorical_cols and len(categorical_cols) >= 2 and len(resolved.years) >= 2 and not resolved.top_n:
        by_year: dict[int, float] = {}
        for r in rows:
            y, v = r.get("sale_year"), r.get(metric_col)
            if y is not None and v is not None:
                by_year[y] = by_year.get(y, 0.0) + float(v)
        if len(by_year) == 2:
            (y1, v1), (y2, v2) = sorted(by_year.items())
            trend = ""
            if v1:
                pct = (v2 - v1) / v1 * 100
                trend = f" - {'up' if pct >= 0 else 'down'} {abs(pct):.1f}%"
            # label.capitalize() rather than a prefixed "Total {label}" - the known
            # metric labels already start with "total" ("total revenue", "total units
            # sold"), so prefixing another one produced "Total total revenue was...".
            sentences.append(
                f"{label.capitalize()} was {currency_prefix}{fmt(v1)} in {y1} and "
                f"{currency_prefix}{fmt(v2)} in {y2}{trend}."
            )

    return NarrativeResult(text=" ".join(sentences), formatting=formatting)
