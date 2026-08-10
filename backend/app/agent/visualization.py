"""Builds the typed chart config from the SQL result set. Deliberately conservative:
render_chart is false whenever there's no categorical dimension to plot against (a single
scalar aggregate has nothing meaningful to chart), rather than fabricating a one-bar chart.
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
    chart_type: str | None = None
    title: str | None = None
    x_axis_key: str | None = None
    y_axis_key: str | None = None
    data: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "render_chart": self.render_chart,
            "chart_type": self.chart_type,
            "title": self.title,
            "x_axis_key": self.x_axis_key,
            "y_axis_key": self.y_axis_key,
            "data": self.data,
        }


def build_visualization(question: str, columns: list[str], rows: list[dict]) -> VisualizationConfig:
    if not rows:
        return VisualizationConfig(render_chart=False)

    numeric_cols = [c for c in columns if _is_numeric(c, rows[0].get(c))]
    categorical_cols = [c for c in columns if c not in numeric_cols]

    if not numeric_cols or not categorical_cols:
        return VisualizationConfig(render_chart=False)

    x_key = categorical_cols[0]
    y_key = numeric_cols[-1]

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
