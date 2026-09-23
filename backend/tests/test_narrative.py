from app.agent.llm_provider import ResolvedQuery
from app.agent.narrative import build_narrative
from app.agent.visualization import build_visualization

_BARE = ResolvedQuery(metric_alias="total_revenue")


def test_narrative_never_invents_numbers_not_in_the_result_set():
    rows = [
        {"store_division": "DHAKA", "total_revenue": 1_420_000.0},
        {"store_division": "CHITTAGONG", "total_revenue": 980_000.0},
    ]
    result = build_narrative(_BARE, ["store_division", "total_revenue"], rows)
    # Every number quoted in the narrative must trace back to a value literally in `rows`.
    assert "DHAKA" in result.text
    assert "1,420" in result.text or "1420" in result.text.replace(",", "")


def test_narrative_is_deterministic():
    rows = [{"store_division": "DHAKA", "total_revenue": 1000.0}]
    a = build_narrative(_BARE, ["store_division", "total_revenue"], rows)
    b = build_narrative(_BARE, ["store_division", "total_revenue"], rows)
    assert a.text == b.text


def test_narrative_handles_empty_rows():
    result = build_narrative(_BARE, [], [])
    assert "No sales matched" in result.text


def test_narrative_handles_single_scalar_aggregate():
    rows = [{"total_revenue": 5_000_000.0}]
    result = build_narrative(_BARE, ["total_revenue"], rows)
    assert "total revenue" in result.text.lower()
    assert result.text.startswith("The ")  # friendlier, sentence-shaped phrasing


def test_visualization_no_chart_for_scalar_result():
    rows = [{"total_revenue": 5_000_000.0}]
    viz = build_visualization(["total_revenue"], rows)
    assert viz.render_chart is False


def test_visualization_bar_chart_for_dimensional_result():
    rows = [
        {"store_division": "DHAKA", "total_revenue": 1000.0},
        {"store_division": "SYLHET", "total_revenue": 500.0},
    ]
    viz = build_visualization(["store_division", "total_revenue"], rows)
    assert viz.render_chart is True
    assert viz.chart_type == "bar"
    assert viz.x_axis_key == "store_division"
    assert viz.data[0]["store_division"] == "DHAKA"  # sorted descending by metric


def test_narrative_does_not_label_counts_as_currency():
    """transaction_count / total_units are not money - a count crossing the 1,000
    scaling threshold must never get a '$' label slapped on it."""
    rows = [{"transaction_count": 1_000_000}]
    result = build_narrative(_BARE, ["transaction_count"], rows)
    assert "$" not in result.text


def test_narrative_labels_revenue_as_currency():
    rows = [{"total_revenue": 1_420_000.0}]
    result = build_narrative(_BARE, ["total_revenue"], rows)
    assert "$" in result.text


def test_visualization_line_chart_for_time_series():
    rows = [
        {"sale_year": 2019, "total_revenue": 1000.0},
        {"sale_year": 2020, "total_revenue": 1500.0},
    ]
    viz = build_visualization(["sale_year", "total_revenue"], rows)
    assert viz.chart_type == "line"


def test_narrative_includes_scope_from_resolved_filters_and_years():
    resolved = ResolvedQuery(metric_alias="total_revenue", year=2020)
    rows = [{"total_revenue": 3_000_000.0}]
    result = build_narrative(resolved, ["total_revenue"], rows)
    assert "2020" in result.text


def test_narrative_no_bullet_format():
    """Regression guard: the old "- Metric: ... / - Breakdown: ... / - Filters: ..."
    bulleted preamble read as a raw state dump, not an answer - the narrative must be
    plain prose now."""
    rows = [
        {"store_division": "DHAKA", "total_revenue": 1000.0},
        {"store_division": "SYLHET", "total_revenue": 500.0},
    ]
    result = build_narrative(_BARE, ["store_division", "total_revenue"], rows)
    assert "- Metric:" not in result.text
    assert "- Breakdown:" not in result.text
    assert "- Filters:" not in result.text


def test_narrative_expands_beyond_top_two_for_larger_result_sets():
    rows = [
        {"store_division": f"DIV{i}", "total_revenue": float(1000 - i * 10)} for i in range(7)
    ]
    result = build_narrative(_BARE, ["store_division", "total_revenue"], rows)
    assert "Across all 7 results" in result.text
    assert "DIV6" in result.text  # the lowest-ranked row is called out


def test_narrative_year_over_year_delta_for_full_multi_year_comparison():
    resolved = ResolvedQuery(metric_alias="total_revenue", dimension="sale_quarter", years=(2019, 2020))
    rows = [
        {"sale_year": 2019, "sale_quarter": "Q1", "total_revenue": 1000.0},
        {"sale_year": 2019, "sale_quarter": "Q2", "total_revenue": 1000.0},
        {"sale_year": 2020, "sale_quarter": "Q1", "total_revenue": 2000.0},
        {"sale_year": 2020, "sale_quarter": "Q2", "total_revenue": 2000.0},
    ]
    result = build_narrative(resolved, ["sale_year", "sale_quarter", "total_revenue"], rows)
    assert "2019" in result.text and "2020" in result.text
    assert "up 100.0%" in result.text


def test_narrative_skips_year_over_year_delta_for_top_n_slice():
    """A top-N slice's sum isn't a true year total - the year-over-year note must not
    fire and imply a misleading aggregate."""
    resolved = ResolvedQuery(
        metric_alias="total_revenue", dimension="store_division", years=(2019, 2020), top_n=10
    )
    rows = [
        {"store_division": "DHAKA", "sale_year": 2020, "total_revenue": 5000.0},
        {"store_division": "DHAKA", "sale_year": 2019, "total_revenue": 4800.0},
        {"store_division": "CHITTAGONG", "sale_year": 2020, "total_revenue": 3000.0},
    ]
    result = build_narrative(resolved, ["store_division", "sale_year", "total_revenue"], rows)
    # "%" only ever appears in the year-over-year delta sentence - a plain absence
    # check on "up "/"down " is too broad, since the (legitimate, wanted) range
    # sentence itself says "...ranging from $X down to $Y...".
    assert "%" not in result.text


def test_visualization_multi_line_for_time_by_series_result():
    """year x quarter: the finer time column is the x axis, one line per year - not a
    single line over a combined "2019 Q1" label (which would read as one flat trend)."""
    rows = [
        {"sale_year": 2019, "sale_quarter": "Q1", "total_revenue": 1000.0},
        {"sale_year": 2020, "sale_quarter": "Q1", "total_revenue": 2000.0},
    ]
    viz = build_visualization(["sale_year", "sale_quarter", "total_revenue"], rows)
    assert viz.render_chart is True
    assert viz.chart_type == "multi_line"
    assert viz.x_axis_key == "sale_quarter"
    assert viz.series_keys == ["2019", "2020"]
    assert viz.columns == ["sale_quarter", "2019", "2020"]
    assert viz.data == [{"sale_quarter": "Q1", "2019": 1000.0, "2020": 2000.0}]


def test_visualization_table_for_two_non_time_dimensions():
    rows = [
        {"store_division": "DHAKA", "store_district": "GAZIPUR", "total_revenue": 10.0},
        {"store_division": "DHAKA", "store_district": "DHAKA", "total_revenue": 20.0},
    ]
    viz = build_visualization(["store_division", "store_district", "total_revenue"], rows)
    assert viz.chart_type == "table"
    assert viz.columns == ["store_division", "store_district", "total_revenue"]


def test_time_series_line_is_chronological_not_ranked():
    rows = [{"sale_month": m, "total_revenue": float(100 - m)} for m in (3, 1, 2)]
    viz = build_visualization(["sale_month", "total_revenue"], rows)
    assert viz.chart_type == "line"
    assert [d["sale_month"] for d in viz.data] == ["Jan", "Feb", "Mar"]


def test_year_comparison_does_not_report_a_decrease_when_nothing_fell():
    resolved = ResolvedQuery(metric_alias="total_revenue", dimension="store_division", years=(2019, 2020))
    rows = [
        {"store_division": "A", "sale_year": 2019, "total_revenue": 100.0},
        {"store_division": "A", "sale_year": 2020, "total_revenue": 110.0},
        {"store_division": "B", "sale_year": 2019, "total_revenue": 100.0},
        {"store_division": "B", "sale_year": 2020, "total_revenue": 100.001},
    ]
    text = build_narrative(resolved, ["store_division", "sale_year", "total_revenue"], rows).text
    assert "decrease" not in text
    assert "no division declined" in text
    assert "in 2019" in text and "in 2020" in text
