"""Tests for fetch_funding parsers — FUNDING.yml and funding.json."""

import json

from src.github.fetch_funding import parse_funding_json, parse_funding_yml


class TestParseFundingYml:
    def test_block_list_at_column_zero(self):
        """A `custom:` key with `- url` items at column 0 is the GitHub
        block form. Regression: the old parser read each `- https://...`
        line as a `key: value` pair, emitting a bogus `- https` platform
        and dropping the real `custom` key.
        """
        text = (
            "open_collective: aiohttp\n"
            "tidelift: pypi/aiohttp\n"
            "custom:\n"
            "- https://opencollective.com/aiohttp\n"
            "- https://www.patreon.com/aiohttp\n"
        )
        parsed = parse_funding_yml(text)
        assert set(parsed) == {"open_collective", "tidelift", "custom"}
        assert "- https" not in parsed
        assert parsed["custom"] == [
            "https://opencollective.com/aiohttp",
            "https://www.patreon.com/aiohttp",
        ]

    def test_block_list_indented(self):
        text = "custom:\n  - https://example.com\n  - https://other.com\n"
        parsed = parse_funding_yml(text)
        assert parsed["custom"] == ["https://example.com", "https://other.com"]

    def test_scalar_value(self):
        assert parse_funding_yml("github: octocat\n") == {"github": "octocat"}

    def test_inline_list(self):
        assert parse_funding_yml("github: [alice, bob]\n") == {
            "github": ["alice", "bob"]
        }

    def test_comments_ignored(self):
        text = "# fund us\ngithub: octocat  # main account\n"
        assert parse_funding_yml(text) == {"github": "octocat"}

    def test_empty_value_dropped(self):
        """An unset platform key (null value) is not a funding signal."""
        text = "github: octocat\npatreon:\nopen_collective: \n"
        assert parse_funding_yml(text) == {"github": "octocat"}

    def test_malformed_yaml_returns_empty(self):
        assert parse_funding_yml("github: [unclosed\n  : :\n") == {}

    def test_non_mapping_returns_empty(self):
        assert parse_funding_yml("- just\n- a\n- list\n") == {}

    def test_empty_text(self):
        assert parse_funding_yml("") == {}


def _manifest(history):
    """A minimal funding.json manifest with the given funding.history."""
    return json.dumps({"version": "v1.0.0", "funding": {"history": history}})


class TestParseFundingJson:
    def test_sums_income_within_2021_2025(self):
        text = _manifest([
            {"year": 2022, "income": 10000, "currency": "USD"},
            {"year": 2023, "income": 25000, "currency": "USD"},
            {"year": 2025, "income": 5000, "currency": "USD"},
        ])
        is_obj, funding_5y = parse_funding_json(text)
        assert is_obj is True
        assert funding_5y == "40000"

    def test_years_outside_window_excluded(self):
        text = _manifest([
            {"year": 2018, "income": 999999},
            {"year": 2020, "income": 888888},
            {"year": 2024, "income": 7000},
            {"year": 2030, "income": 777777},
        ])
        assert parse_funding_json(text)[1] == "7000"

    def test_string_years_accepted(self):
        text = _manifest([{"year": "2023", "income": "1500"}])
        assert parse_funding_json(text)[1] == "1500"

    def test_float_income_formatted(self):
        text = _manifest([{"year": 2024, "income": 1234.5}])
        assert parse_funding_json(text)[1] == "1234.50"

    def test_entries_without_income_skipped(self):
        text = _manifest([
            {"year": 2023, "expenses": 5000},          # no income
            {"year": 2024, "income": 800},
        ])
        assert parse_funding_json(text)[1] == "800"

    def test_no_history_is_valid_but_empty(self):
        """openssl/openssl is the real-world shape: channels+plans, no history."""
        text = json.dumps({"version": "v1.0.0",
                            "funding": {"channels": [], "plans": []}})
        assert parse_funding_json(text) == (True, "")

    def test_no_funding_block(self):
        assert parse_funding_json(json.dumps({"version": "v1.0.0"})) == (True, "")

    def test_history_present_but_all_out_of_window(self):
        assert parse_funding_json(_manifest([{"year": 2019, "income": 100}])) == (True, "")

    def test_malformed_json(self):
        assert parse_funding_json("{not valid json") == (False, "")

    def test_non_object_json(self):
        assert parse_funding_json("[1, 2, 3]") == (False, "")

    def test_empty_text(self):
        assert parse_funding_json("") == (False, "")
