"""Tests for src/github/fetch_funding_yml.py — FUNDING.yml parsing + github logins."""

from src.github.fetch_funding_yml import parse_funding_yml, funding_yml_github_logins


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


class TestGithubLogins:
    def test_from_scalar(self):
        assert funding_yml_github_logins({"github": "Alice"}) == ["alice"]

    def test_from_list(self):
        assert funding_yml_github_logins({"github": ["Alice", "bob"]}) == ["alice", "bob"]

    def test_no_github_key(self):
        assert funding_yml_github_logins({"patreon": "x"}) == []

    def test_dedupe_preserves_order(self):
        assert funding_yml_github_logins({"github": ["a", "A", "b"]}) == ["a", "b"]
