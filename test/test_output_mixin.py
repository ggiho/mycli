"""Tests for output_mixin: column truncation and null handling."""

from mycli.output_mixin import _truncate_columns


class TestTruncateColumns:
    def test_no_truncation_when_disabled(self):
        headers = ["name", "value"]
        data = [("short", "data")]
        result_data, result_headers = _truncate_columns(data, headers, max_width=0)
        assert list(result_data) == [("short", "data")]
        assert result_headers == headers

    def test_no_truncation_when_under_limit(self):
        headers = ["name"]
        data = [("hello",)]
        result_data, result_headers = _truncate_columns(data, headers, max_width=50)
        assert list(result_data) == [("hello",)]

    def test_truncation_at_boundary(self):
        headers = ["col"]
        long_val = "a" * 20
        data = [(long_val,)]
        result_data, _ = _truncate_columns(data, headers, max_width=10)
        rows = list(result_data)
        assert len(rows) == 1
        truncated = rows[0][0]
        assert truncated == "aaaaaaa..."
        assert len(truncated) == 10

    def test_truncation_preserves_non_string_values(self):
        headers = ["id", "name"]
        data = [(42, "a" * 100)]
        result_data, _ = _truncate_columns(data, headers, max_width=15)
        rows = list(result_data)
        assert rows[0][0] == 42  # int preserved
        assert rows[0][1].endswith("...")
        assert len(rows[0][1]) == 15

    def test_truncation_multiple_rows(self):
        headers = ["val"]
        data = [("short",), ("a" * 50,), ("medium text",)]
        result_data, _ = _truncate_columns(data, headers, max_width=10)
        rows = list(result_data)
        assert rows[0] == ("short",)
        assert rows[1][0] == "aaaaaaa..."
        assert rows[2] == ("medium tex...",) or len(rows[2][0]) <= 13

    def test_truncation_with_none_values(self):
        headers = ["val"]
        data = [(None,), ("a" * 50,)]
        result_data, _ = _truncate_columns(data, headers, max_width=10)
        rows = list(result_data)
        assert rows[0] == (None,)
        assert rows[1][0] == "aaaaaaa..."

    def test_negative_max_width_disables(self):
        headers = ["val"]
        data = [("long value here",)]
        result_data, _ = _truncate_columns(data, headers, max_width=-1)
        assert list(result_data) == [("long value here",)]

    def test_headers_pass_through(self):
        headers = ["col1", "col2", "col3"]
        data = [("a", "b", "c")]
        _, result_headers = _truncate_columns(data, headers, max_width=50)
        assert result_headers == ["col1", "col2", "col3"]

    def test_very_small_max_width(self):
        headers = ["val"]
        data = [("hello world",)]
        result_data, _ = _truncate_columns(data, headers, max_width=4)
        rows = list(result_data)
        # cutoff = max(4 - 3, 1) = 1, so "h..."
        assert rows[0][0] == "h..."


class TestTruncateColumnsEdgeCases:
    def test_empty_data(self):
        headers = ["val"]
        data = []
        result_data, result_headers = _truncate_columns(data, headers, max_width=10)
        assert list(result_data) == []
        assert result_headers == ["val"]

    def test_empty_string_value(self):
        headers = ["val"]
        data = [("",)]
        result_data, _ = _truncate_columns(data, headers, max_width=10)
        rows = list(result_data)
        assert rows[0] == ("",)

    def test_exact_max_width_string(self):
        headers = ["val"]
        data = [("1234567890",)]
        result_data, _ = _truncate_columns(data, headers, max_width=10)
        rows = list(result_data)
        assert rows[0] == ("1234567890",)  # exactly at limit, no truncation
