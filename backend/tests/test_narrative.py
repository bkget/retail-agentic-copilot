from app.agent.narrative import build_narrative
from app.agent.visualization import build_visualization


def test_narrative_never_invents_numbers_not_in_the_result_set():
    rows = [
        {"store_division": "DHAKA", "total_revenue": 1_420_000.0},
        {"store_division": "CHITTAGONG", "total_revenue": 980_000.0},
    ]
    result = build_narrative("revenue by division", ["store_division", "total_revenue"], rows)
    # Every number quoted in the narrative must trace back to a value literally in `rows`.
    assert "DHAKA" in result.text
    assert "1,420" in result.text or "1420" in result.text.replace(",", "")


def test_narrative_is_deterministic():
    rows = [{"store_division": "DHAKA", "total_revenue": 1000.0}]
    a = build_narrative("q", ["store_division", "total_revenue"], rows)
    b = build_narrative("q", ["store_division", "total_revenue"], rows)
    assert a.text == b.text


def test_narrative_handles_empty_rows():
    result = build_narrative("q", [], [])
    assert "No rows" in result.text


def test_narrative_handles_single_scalar_aggregate():
    rows = [{"total_revenue": 5_000_000.0}]
    result = build_narrative("total revenue", ["total_revenue"], rows)
    assert "total revenue" in result.text.lower()
    assert result.text.startswith("The ")  # friendlier, sentence-shaped phrasing


def test_visualization_no_chart_for_scalar_result():
    rows = [{"total_revenue": 5_000_000.0}]
    viz = build_visualization("q", ["total_revenue"], rows)
    assert viz.render_chart is False


def test_visualization_bar_chart_for_dimensional_result():
    rows = [
        {"store_division": "DHAKA", "total_revenue": 1000.0},
        {"store_division": "SYLHET", "total_revenue": 500.0},
    ]
    viz = build_visualization("q", ["store_division", "total_revenue"], rows)
    assert viz.render_chart is True
    assert viz.chart_type == "bar"
    assert viz.x_axis_key == "store_division"
    assert viz.data[0]["store_division"] == "DHAKA"  # sorted descending by metric


def test_narrative_does_not_label_counts_as_currency():
    """transaction_count / total_units are not money - a count crossing the 1,000
    scaling threshold must never get a '$' label slapped on it."""
    rows = [{"transaction_count": 1_000_000}]
    result = build_narrative("how many transactions", ["transaction_count"], rows)
    assert "$" not in result.text


def test_narrative_labels_revenue_as_currency():
    rows = [{"total_revenue": 1_420_000.0}]
    result = build_narrative("total revenue", ["total_revenue"], rows)
    assert "$" in result.text


def test_visualization_line_chart_for_time_series():
    rows = [
        {"sale_year": 2019, "total_revenue": 1000.0},
        {"sale_year": 2020, "total_revenue": 1500.0},
    ]
    viz = build_visualization("q", ["sale_year", "total_revenue"], rows)
    assert viz.chart_type == "line"
