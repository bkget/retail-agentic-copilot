"""Deterministic narrative generation. All numbers quoted in the narrative are read
directly from the SQL result set - never re-computed or paraphrased by an LLM - so the
narrative can never say a number that isn't literally in the data the guardrailed query
returned. This is the mechanism behind the spec's "no math hallucinations" requirement.

Works generically off the shape of the result set (which columns are numeric vs.
categorical) rather than needing structured intent from the SQL-generation step, so it
works the same whether SQL came from MockLLMProvider or a real LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Number

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


def build_narrative(question: str, columns: list[str], rows: list[dict]) -> NarrativeResult:
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

    if not categorical_cols or len(rows) == 1:
        total = sum(values)
        text = f"The {label} is {currency_prefix}{fmt(total)}."
        return NarrativeResult(text=text, formatting=formatting)

    dim_col = categorical_cols[0]
    dim_label = dim_col.replace("_", " ")
    ranked = sorted(rows, key=lambda r: (r.get(metric_col) or 0), reverse=True)
    leader = ranked[0]
    text = (
        f"Looking at {label} by {dim_label}, **{leader[dim_col]}** leads with "
        f"{currency_prefix}{fmt(leader[metric_col])}"
    )
    if len(ranked) > 1:
        runner_up = ranked[1]
        text += f", followed by **{runner_up[dim_col]}** with {currency_prefix}{fmt(runner_up[metric_col])}"
    text += "."
    return NarrativeResult(text=text, formatting=formatting)
