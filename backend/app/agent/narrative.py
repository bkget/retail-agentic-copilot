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

import calendar
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


_TIME_COLUMNS = ("sale_month", "sale_quarter", "sale_year", "sale_date")


def format_dimension_value(column: str, value: object) -> str:
    """Month numbers read as months ("December", not "12") everywhere they're quoted."""
    if column == "sale_month" and isinstance(value, int) and 1 <= value <= 12:
        return calendar.month_name[value]
    return str(value)


def _with_notes(text: str, resolved: ResolvedQuery) -> str:
    if not resolved.notes:
        return text
    return text + "\n\n" + " ".join(f"Note: {n}" for n in resolved.notes)


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
    period = ""
    if resolved.year is None and len(resolved.years) < 2 and resolved.period_label:
        period = f" over {resolved.period_label}"
    if not parts:
        return period
    return " for " + ", ".join(parts) + period


def _entity_label(row: dict, key_cols: list[str]) -> str:
    if key_cols == ["store_division", "store_district"]:
        return f"{row['store_district']} ({row['store_division']})"
    return " ".join(format_dimension_value(c, row[c]) for c in key_cols)


def _year_comparison_narrative(
    resolved: ResolvedQuery, rows: list[dict], key_cols: list[str], metric_col: str, fmt,
    currency_prefix: str, label: str,
) -> str:
    """"Compare 2019 and 2020 by X": every figure is stated with its year, plus the
    overall change and the biggest movers - never a single number summed across years."""
    years = sorted({r["sale_year"] for r in rows if r.get("sale_year") is not None})
    first, last = years[0], years[-1]
    per_entity: dict[str, dict[int, float]] = {}
    per_year: dict[int, float] = {}
    for r in rows:
        v = r.get(metric_col)
        if v is None:
            continue
        name = _entity_label(r, key_cols)
        per_entity.setdefault(name, {})[r["sale_year"]] = float(v)
        per_year[r["sale_year"]] = per_year.get(r["sale_year"], 0.0) + float(v)

    dims = "district (within each division)" if key_cols == ["store_division", "store_district"] else \
        " and ".join(c.replace("store_", "").replace("item_", "").replace("_", " ") for c in key_cols)
    money = lambda v: f"{currency_prefix}{fmt(v)}"  # noqa: E731
    year_list = " and ".join(str(y) for y in years)
    sentences: list[str] = []
    if not resolved.top_n and first in per_year and last in per_year:
        change = ""
        if per_year[first]:
            pct = (per_year[last] - per_year[first]) / per_year[first] * 100
            change = f" ({'up' if pct >= 0 else 'down'} {abs(pct):.1f}%)"
        sentences.append(
            f"Comparing {label} by {dims} for {year_list}: overall it went from "
            f"{money(per_year[first])} in {first} to {money(per_year[last])} in {last}{change}."
        )
    else:
        sentences.append(f"Comparing {label} by {dims} for {year_list}.")

    leaders = sorted(per_entity.items(), key=lambda kv: kv[1].get(last, 0.0), reverse=True)
    if leaders:
        name, vals = leaders[0]
        prev = f" (vs {money(vals[first])} in {first})" if first in vals else ""
        sentences.append(f"**{name}** was the highest in {last} with {money(vals.get(last, 0.0))}{prev}.")

    if len(years) == 2 and not resolved.top_n:
        changes = [
            (n, (v[last] - v[first]) / v[first] * 100)
            for n, v in per_entity.items() if v.get(first) and last in v
        ]
        if len(changes) >= 2:
            # Changes that round to 0.0% are "flat", not an increase/decrease - saying
            # "biggest decrease: X (-0.0%)" when nothing fell would mislead.
            up = max(changes, key=lambda c: c[1])
            down = min(changes, key=lambda c: c[1])
            unit = dims.split(" (")[0]
            parts = []
            if up[1] >= 0.05:
                parts.append(f"Biggest increase: **{up[0]}** ({up[1]:+.1f}%)")
            if down[1] <= -0.05:
                parts.append(f"biggest decrease: **{down[0]}** ({down[1]:+.1f}%)")
            elif up[1] >= 0.05:
                parts.append(f"no {unit} declined")
            if not parts:
                parts.append(f"Every {unit} was essentially flat between {first} and {last}")
            text = "; ".join(parts)
            sentences.append(text[0].upper() + text[1:] + ".")
    return " ".join(sentences)


def _series_by_time_narrative(
    resolved: ResolvedQuery, rows: list[dict], metric_col: str, fmt, currency_prefix: str, label: str
) -> str:
    """<entity> x <time> results (e.g. revenue per district per month): rank the
    entities by their total across the period, then call out the strongest and weakest
    period across all of them - the two things people actually read such a chart for."""
    dim, time_col = resolved.dimension, resolved.extra_dimension
    per_series: dict[str, float] = {}
    per_period: dict[object, float] = {}
    for r in rows:
        v = r.get(metric_col)
        if v is None:
            continue
        per_series[str(r.get(dim))] = per_series.get(str(r.get(dim)), 0.0) + float(v)
        per_period[r.get(time_col)] = per_period.get(r.get(time_col), 0.0) + float(v)
    ranked = sorted(per_series.items(), key=lambda kv: kv[1], reverse=True)
    dim_label = dim.replace("store_", "").replace("item_", "").replace("_", " ")
    time_label = time_col.replace("sale_", "")
    scope = _describe_scope(resolved)
    limited = (
        f" (showing the top {resolved.series_limit} {dim_label}s by {label})"
        if resolved.series_limit and len(ranked) >= resolved.series_limit
        else ""
    )
    sentences = [
        f"Looking at {label} by {dim_label} and {time_label}{scope}{limited}, "
        f"**{ranked[0][0]}** leads with {currency_prefix}{fmt(ranked[0][1])} in total"
        + (f", followed by **{ranked[1][0]}** at {currency_prefix}{fmt(ranked[1][1])}" if len(ranked) > 1 else "")
        + "."
    ]
    if len(per_period) >= 2:
        best = max(per_period.items(), key=lambda kv: kv[1])
        worst = min(per_period.items(), key=lambda kv: kv[1])
        sentences.append(
            f"Across these {len(ranked)} {dim_label}s, the strongest {time_label} was "
            f"**{format_dimension_value(time_col, best[0])}** ({currency_prefix}{fmt(best[1])}) "
            f"and the weakest was **{format_dimension_value(time_col, worst[0])}** "
            f"({currency_prefix}{fmt(worst[1])})."
        )
    return " ".join(sentences)


def build_narrative(resolved: ResolvedQuery, columns: list[str], rows: list[dict]) -> NarrativeResult:
    if not rows:
        return NarrativeResult(
            text=_with_notes(
                "No sales matched this question - the filters may be too narrow "
                "(for example, a name that doesn't appear in the data).",
                resolved,
            ),
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
        return NarrativeResult(text=_with_notes(text, resolved), formatting=formatting)

    others = [c for c in categorical_cols if c != "sale_year"]
    if (
        "sale_year" in categorical_cols
        and others
        and not any(c in _TIME_COLUMNS for c in others)
        and len(resolved.years) >= 2
    ):
        text = _year_comparison_narrative(resolved, rows, others, metric_col, fmt, currency_prefix, label)
        return NarrativeResult(text=_with_notes(text, resolved), formatting=formatting)

    if (
        resolved.extra_dimension in _TIME_COLUMNS
        and resolved.dimension in categorical_cols
        and resolved.extra_dimension in categorical_cols
        and resolved.dimension not in _TIME_COLUMNS
    ):
        text = _series_by_time_narrative(resolved, rows, metric_col, fmt, currency_prefix, label)
        return NarrativeResult(text=_with_notes(text, resolved), formatting=formatting)

    # Handles 1+ categorical columns uniformly - a single column (the common case)
    # degenerates to exactly the old single-dimension wording; 2+ columns (e.g. a
    # multi-year comparison also broken down by quarter) get a combined label like
    # "2020 Q4" instead of silently describing only the first column and ignoring the
    # rest, which is what used to happen when categorical_cols[0] was the only column
    # ever consulted.
    dim_label = " and ".join(c.replace("_", " ") for c in categorical_cols)
    is_hierarchy = categorical_cols == ["store_division", "store_district"]
    if is_hierarchy:
        dim_label = "district (within each division)"

    def combo_label(row: dict) -> str:
        if is_hierarchy:
            # "NARAIL (KHULNA)", not "KHULNA NARAIL" / "DHAKA DHAKA".
            return f"{row['store_district']} ({row['store_division']})"
        return " ".join(format_dimension_value(c, row[c]) for c in categorical_cols)

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

    return NarrativeResult(text=_with_notes(" ".join(sentences), resolved), formatting=formatting)
