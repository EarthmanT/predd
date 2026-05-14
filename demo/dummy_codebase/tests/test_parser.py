"""Tests for parser.py."""

import pytest
from parser import paginate, parse_csv_line, extract_key_value


class TestParseCsvLine:
    def test_simple(self):
        assert parse_csv_line("a,b,c") == ["a", "b", "c"]

    def test_quoted(self):
        assert parse_csv_line('"hello, world",b') == ["hello, world", "b"]


class TestPaginate:
    def test_first_page(self):
        assert paginate(list(range(10)), page=0, page_size=5) == [0, 1, 2, 3, 4]

    # NOTE: this test currently fails due to the off-by-one bug (DEMO-11)
    @pytest.mark.xfail(reason="DEMO-11: off-by-one bug not yet fixed")
    def test_last_page(self):
        assert paginate(list(range(10)), page=1, page_size=5) == [5, 6, 7, 8, 9]


class TestExtractKeyValue:
    def test_extracts(self):
        assert extract_key_value("capability: platform-api\nother: stuff", "capability") == "platform-api"

    def test_missing(self):
        assert extract_key_value("no capability here", "capability") == ""
