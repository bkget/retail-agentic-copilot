"""Pure-function unit tests for named-value filter extraction and SQL escaping - no DB
needed, `SchemaCatalog` is constructed directly with fake enum data. See
app/agent/llm_provider.py::_extract_named_filters for the full design rationale.
"""

from app.agent.llm_provider import (
    _extract_named_filters,
    _extract_quoted_values,
    _NamedFilter,
    _sql_escape,
)
from app.schema.catalog import SchemaCatalog

FAKE_CATALOG = SchemaCatalog(
    tables={},
    enum_hints={
        "store_division": ["BARISAL", "CHITTAGONG", "DHAKA", "KHULNA", "RAJSHAHI", "RANGPUR", "SYLHET"],
        "payment_type": ["card", "cash", "mobile"],
        "sale_quarter": ["Q1", "Q2", "Q3", "Q4"],
        "item_manufacturer_country": [
            "Bangladesh", "Cambodia", "China", "Finland", "Germany",
            "India", "Lithuania", "Netherlands", "poland", "United States",
        ],
    },
)


def test_sql_escape_doubles_single_quotes():
    assert _sql_escape("O'Brien") == "O''Brien"
    assert _sql_escape("no quotes here") == "no quotes here"


def test_named_filter_to_sql_equals():
    f = _NamedFilter("store_division", "=", ("DHAKA",))
    assert f.to_sql() == "store_division = 'DHAKA'"


def test_named_filter_to_sql_equals_escapes_embedded_quote():
    f = _NamedFilter("item_name", "=", ("O'Brien's",))
    assert f.to_sql() == "item_name = 'O''Brien''s'"


def test_named_filter_to_sql_in():
    f = _NamedFilter("store_division", "IN", ("DHAKA", "CHITTAGONG"))
    assert f.to_sql() == "store_division IN ('DHAKA', 'CHITTAGONG')"


def test_named_filter_to_sql_ilike_single():
    f = _NamedFilter("item_name", "ILIKE", ("Pepsi",))
    assert f.to_sql() == "item_name ILIKE '%Pepsi%'"


def test_named_filter_to_sql_ilike_multi_ors():
    f = _NamedFilter("item_name", "ILIKE", ("Pepsi", "Sprite"))
    assert f.to_sql() == "(item_name ILIKE '%Pepsi%' OR item_name ILIKE '%Sprite%')"


def test_extract_quoted_values_single_and_double_quotes():
    assert _extract_quoted_values("revenue for 'Dhaka' and \"Sylhet\"") == ["Dhaka", "Sylhet"]


def test_extract_quoted_values_none_present():
    assert _extract_quoted_values("total revenue by division") == []


class TestExtractNamedFilters:
    def test_single_division_match_is_an_equals_filter_and_consumed(self):
        filters, consumed, compare = _extract_named_filters(
            "revenue for the 'Dhaka' division", FAKE_CATALOG
        )
        assert filters == [_NamedFilter("store_division", "=", ("DHAKA",))]
        assert consumed == {"store_division"}
        assert compare is None

    def test_bare_unquoted_enum_value_matches(self):
        """"cash" without quotes must still resolve - not every named value in a real
        question comes quoted."""
        filters, consumed, _ = _extract_named_filters(
            "revenue for cash transactions", FAKE_CATALOG
        )
        assert filters == [_NamedFilter("payment_type", "=", ("cash",))]
        assert consumed == {"payment_type"}

    def test_case_insensitive_match_uses_canonical_stored_value(self):
        """Matching against the real man_country casing quirk (stored as lowercase
        "poland"): the question can say "Poland" naturally, the generated filter must
        use the value as actually stored, or the query would silently match nothing."""
        filters, _, _ = _extract_named_filters(
            "revenue for items manufactured in 'Poland'", FAKE_CATALOG
        )
        assert filters == [_NamedFilter("item_manufacturer_country", "=", ("poland",))]

    def test_two_values_same_dimension_triggers_compare_mode(self):
        filters, consumed, compare = _extract_named_filters(
            "compare 'Dhaka' and 'Chittagong'", FAKE_CATALOG
        )
        # Match order follows catalog.enum_hints order (alphabetical, from `ORDER BY 1`
        # in load_catalog), not the order the values appear in the question text.
        assert filters == [_NamedFilter("store_division", "IN", ("CHITTAGONG", "DHAKA"))]
        assert compare == "store_division"
        assert consumed == {"store_division"}

    def test_unmatched_quoted_value_becomes_item_name_ilike(self):
        filters, consumed, compare = _extract_named_filters(
            "revenue for 'Red Bull 12oz'", FAKE_CATALOG
        )
        assert filters == [_NamedFilter("item_name", "ILIKE", ("Red Bull 12oz",))]
        assert "item_name" not in consumed  # item_name was never a candidate group-by anyway
        assert compare is None

    def test_two_unmatched_quoted_values_trigger_item_name_compare(self):
        filters, _, compare = _extract_named_filters(
            "compare 'Pepsi - 12 oz cans' and 'Sprite - 12 oz cans'", FAKE_CATALOG
        )
        assert compare == "item_name"
        assert filters == [
            _NamedFilter("item_name", "ILIKE", ("Pepsi - 12 oz cans", "Sprite - 12 oz cans"))
        ]

    def test_enum_match_takes_priority_over_item_name_fallback_for_same_text(self):
        """A quoted value that DOES match a known enum must not also become a
        redundant item_name filter."""
        filters, _, _ = _extract_named_filters("revenue for 'Dhaka'", FAKE_CATALOG)
        assert len(filters) == 1
        assert filters[0].column == "store_division"

    def test_no_named_values_returns_empty(self):
        filters, consumed, compare = _extract_named_filters(
            "total revenue by division", FAKE_CATALOG
        )
        assert filters == []
        assert consumed == set()
        assert compare is None
