"""Builds the typed chart/table config from the SQL result set. Deliberately
conservative: render_chart is false whenever there's no categorical dimension to plot
against (a single scalar aggregate has nothing meaningful to chart), rather than
fabricating a one-bar chart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Number

MAX_CHART_ROWS = 25

# Kept in sync with app.agent.narrative - see that module for why these integer-typed
# columns must be treated as dimensions, not measures.
_DIMENSION_LIKE_NUMERIC_COLUMNS = {"sale_year", "sale_month", "sale_day"}


def _is_numeric(column: str, value: object) -> bool:
    if column in _DIMENSION_LIKE_NUMERIC_COLUMNS:
        return False
    return isinstance(value, Number) and not isinstance(value, bool)


@dataclass(frozen=True)
class VisualizationConfig:
    render_chart: bool
    chart_type: str | None = None  # "bar" | "line" | "table"
    title: str | None = None
    x_axis_key: str | None = None  # bar/line only
    y_axis_key: str | None = None  # bar/line only
    columns: list[str] = field(default_factory=list)  # table only: header order
    data: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "render_chart": self.render_chart,
            "chart_type": self.chart_type,
            "title": self.title,
            "x_axis_key": self.x_axis_key,
            "y_axis_key": self.y_axis_key,
            "columns": self.columns,
            "data": self.data,
        }


def build_visualization(columns: list[str], rows: list[dict]) -> VisualizationConfig:
    if not rows:
        return VisualizationConfig(render_chart=False)

    numeric_cols = [c for c in columns if _is_numeric(c, rows[0].get(c))]
    categorical_cols = [c for c in columns if c not in numeric_cols]

    if not numeric_cols or not categorical_cols:
        return VisualizationConfig(render_chart=False)

    y_key = numeric_cols[-1]

    if len(categorical_cols) >= 2:
        # More than one dimension column (e.g. a multi-year comparison also broken
        # down by quarter) - a single x/y chart can't represent two independent
        # dimensions without collapsing them into one combined label ("2020 Q4"),
        # which reads as a single flat trend line rather than a real comparison. A
        # table shows every value precisely instead, which is what this is for.
        table_cols = categorical_cols + [y_key]
        ranked = rows[:MAX_CHART_ROWS]  # preserve the SQL's own ORDER BY - already
        # meaningful (chronological for a time comparison, ranked for a top-N request)
        data = [
            {c: (float(r[c]) if c == y_key and r[c] is not None else r[c]) for c in table_cols}
            for r in ranked
        ]
        title_dims = " & ".join(c.replace("_", " ").title() for c in categorical_cols)
        title = f"{y_key.replace('_', ' ').title()} by {title_dims}"
        return VisualizationConfig(
            render_chart=True,
            chart_type="table",
            title=title,
            columns=table_cols,
            data=data,
        )

    x_key = categorical_cols[0]
    ranked = sorted(rows, key=lambda r: (r.get(y_key) or 0), reverse=True)[:MAX_CHART_ROWS]
    data = [{x_key: r[x_key], y_key: float(r[y_key]) if r[y_key] is not None else None} for r in ranked]
    is_time_series = x_key in {"sale_date", "sale_year", "sale_month", "sale_quarter"}
    chart_type = "line" if is_time_series else "bar"
    title = f"{y_key.replace('_', ' ').title()} by {x_key.replace('_', ' ').title()}"

    return VisualizationConfig(
        render_chart=True,
        chart_type=chart_type,
        title=title,
        x_axis_key=x_key,
        y_axis_key=y_key,
        data=data,
    )
