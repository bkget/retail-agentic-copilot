"""Builds the typed chart/table config from the SQL result set. Deliberately
conservative: render_chart is false whenever there's no categorical dimension to plot
against (a single scalar aggregate has nothing meaningful to chart), rather than
fabricating a one-bar chart.

Chart types:
  * bar         - one categorical dimension, ranked by value
  * line        - one time dimension, in chronological order
  * multi_line  - one time dimension x one series dimension (e.g. revenue per district
                  per month, or per year per quarter), pivoted so each series is a key
  * table       - two non-time dimensions (e.g. division x district)

For multi_line, `columns` is [x_key, *series_keys] and `data` is the pivoted rows, so
the exact same payload renders as a table too (the frontend offers that toggle).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from numbers import Number

MAX_CHART_ROWS = 25
MAX_SERIES = 8
MAX_TABLE_ROWS = 100

# Kept in sync with app.agent.narrative - see that module for why these integer-typed
# columns must be treated as dimensions, not measures.
_DIMENSION_LIKE_NUMERIC_COLUMNS = {"sale_year", "sale_month", "sale_day"}

# Finest first - when two time columns are present (year x quarter), the finer one is
# the x axis and the coarser one becomes the series (one line per year).
_TIME_COLUMNS_FINEST_FIRST = ("sale_date", "sale_month", "sale_quarter", "sale_year")


def _is_numeric(column: str, value: object) -> bool:
    if column in _DIMENSION_LIKE_NUMERIC_COLUMNS:
        return False
    return isinstance(value, Number) and not isinstance(value, bool)


def _time_label(column: str, value: object) -> object:
    if column == "sale_month" and isinstance(value, int) and 1 <= value <= 12:
        return calendar.month_abbr[value]
    return value


def _sort_key(value: object) -> tuple:
    return (0, value) if isinstance(value, (int, float)) else (1, str(value))


def _title(y_key: str, dims: list[str]) -> str:
    dims_text = " & ".join(c.replace("_", " ").title() for c in dims)
    return f"{y_key.replace('_', ' ').title()} by {dims_text}"


@dataclass(frozen=True)
class VisualizationConfig:
    render_chart: bool
    chart_type: str | None = None  # "bar" | "line" | "multi_line" | "grouped_bar" | "table"
    title: str | None = None
    x_axis_key: str | None = None  # bar/line/multi_line
    y_axis_key: str | None = None  # bar/line (metric column); multi_line: metric name
    columns: list[str] = field(default_factory=list)  # table header order
    data: list[dict] = field(default_factory=list)
    series_keys: list[str] = field(default_factory=list)  # multi_line only

    def to_dict(self) -> dict:
        return {
            "render_chart": self.render_chart,
            "chart_type": self.chart_type,
            "title": self.title,
            "x_axis_key": self.x_axis_key,
            "y_axis_key": self.y_axis_key,
            "columns": self.columns,
            "data": self.data,
            "series_keys": self.series_keys,
        }


def _to_float(v: object) -> float | None:
    return float(v) if v is not None else None


def _multi_line(x_key: str, series_key: str, y_key: str, rows: list[dict]) -> VisualizationConfig:
    totals: dict[str, float] = {}
    for r in rows:
        totals[str(r[series_key])] = totals.get(str(r[series_key]), 0.0) + float(r[y_key] or 0)
    # Chronological series (years) stay in order; entity series are ranked by total.
    if series_key in _TIME_COLUMNS_FINEST_FIRST:
        series = sorted(totals, key=_sort_key)[:MAX_SERIES]
    else:
        series = [k for k, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)][:MAX_SERIES]
    kept = set(series)

    pivot: dict[object, dict] = {}
    for r in rows:
        s = str(r[series_key])
        if s not in kept:
            continue
        x = r[x_key]
        pivot.setdefault(x, {x_key: _time_label(x_key, x)})[s] = _to_float(r[y_key])
    data = [pivot[x] for x in sorted(pivot, key=_sort_key)]
    return VisualizationConfig(
        render_chart=True,
        chart_type="multi_line",
        title=_title(y_key, [series_key, x_key]),
        x_axis_key=x_key,
        y_axis_key=y_key,
        columns=[x_key, *series],
        data=data,
        series_keys=series,
    )


MAX_GROUPED_BAR_ENTITIES = 12


def _year_comparison(key_cols: list[str], y_key: str, rows: list[dict]) -> VisualizationConfig:
    """Entity x year results pivoted to one row per entity with a column per year (and
    a change % when exactly two years are compared) - so every value is unambiguously
    tied to its year. Few entities -> grouped bar chart (bars side by side per year);
    many -> a sortable comparison table. Rows are ordered by the latest year, desc."""
    years = sorted({r["sale_year"] for r in rows if r.get("sale_year") is not None})
    year_keys = [str(y) for y in years]
    pivot: dict[tuple, dict] = {}
    for r in rows:
        key = tuple(r[c] for c in key_cols)
        entry = pivot.setdefault(key, {c: r[c] for c in key_cols})
        entry[str(r["sale_year"])] = _to_float(r[y_key])
    change_key = None
    if len(years) == 2:
        change_key = "change_pct"
        a, b = year_keys
        for entry in pivot.values():
            v1, v2 = entry.get(a), entry.get(b)
            entry[change_key] = round((v2 - v1) / v1 * 100, 1) if v1 and v2 is not None else None
    latest = year_keys[-1]
    data = sorted(pivot.values(), key=lambda e: e.get(latest) or 0, reverse=True)
    columns = [*key_cols, *year_keys] + ([change_key] if change_key else [])
    title = f"{y_key.replace('_', ' ').title()}: {' vs '.join(year_keys)} by " + " & ".join(
        c.replace("_", " ").title() for c in key_cols
    )
    if len(key_cols) == 1 and len(data) <= MAX_GROUPED_BAR_ENTITIES:
        return VisualizationConfig(
            render_chart=True, chart_type="grouped_bar", title=title, x_axis_key=key_cols[0],
            y_axis_key=y_key, columns=columns, data=data, series_keys=year_keys,
        )
    # Hierarchy (division > district): chart the finest level, keep both in the table.
    return VisualizationConfig(
        render_chart=True, chart_type="table", title=title, x_axis_key=key_cols[-1],
        y_axis_key=y_key, columns=columns, data=data[:MAX_TABLE_ROWS], series_keys=year_keys,
    )


def build_visualization(columns: list[str], rows: list[dict]) -> VisualizationConfig:
    if not rows:
        return VisualizationConfig(render_chart=False)

    numeric_cols = [c for c in columns if _is_numeric(c, rows[0].get(c))]
    categorical_cols = [c for c in columns if c not in numeric_cols]

    if not numeric_cols or not categorical_cols:
        return VisualizationConfig(render_chart=False)

    y_key = numeric_cols[-1]

    others = [c for c in categorical_cols if c != "sale_year"]
    if (
        "sale_year" in categorical_cols
        and others
        and not any(c in _TIME_COLUMNS_FINEST_FIRST for c in others)
        and len({r.get("sale_year") for r in rows}) >= 2
    ):
        return _year_comparison(others, y_key, rows)

    if len(categorical_cols) == 2:
        time_cols = [c for c in _TIME_COLUMNS_FINEST_FIRST if c in categorical_cols]
        if time_cols:
            x_key = time_cols[0]
            series_key = next(c for c in categorical_cols if c != x_key)
            return _multi_line(x_key, series_key, y_key, rows)

    if len(categorical_cols) >= 2:
        # Two independent non-time dimensions (e.g. division x district): a table
        # shows every value precisely, preserving the SQL's own ORDER BY.
        table_cols = categorical_cols + [y_key]
        data = [
            {c: (_to_float(r[c]) if c == y_key else r[c]) for c in table_cols}
            for r in rows[:MAX_TABLE_ROWS]
        ]
        return VisualizationConfig(
            render_chart=True,
            chart_type="table",
            title=_title(y_key, categorical_cols),
            columns=table_cols,
            data=data,
        )

    x_key = categorical_cols[0]
    is_time_series = x_key in _TIME_COLUMNS_FINEST_FIRST
    if is_time_series:
        # A trend line must be chronological - ranking by value (the old behavior)
        # drew a zig-zag that read as a meaningless "trend".
        ordered = sorted(rows, key=lambda r: _sort_key(r.get(x_key)))[:MAX_CHART_ROWS * 2]
    else:
        ordered = sorted(rows, key=lambda r: (r.get(y_key) or 0), reverse=True)[:MAX_CHART_ROWS]
    data = [{x_key: _time_label(x_key, r[x_key]), y_key: _to_float(r[y_key])} for r in ordered]

    return VisualizationConfig(
        render_chart=True,
        chart_type="line" if is_time_series else "bar",
        title=_title(y_key, [x_key]),
        x_axis_key=x_key,
        y_axis_key=y_key,
        columns=[x_key, y_key],
        data=data,
    )
