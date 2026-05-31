from src.github.fetch_funding_yml import parse_funding_yml, funding_yml_github_logins


def test_parse_block_list_and_custom():
    text = "github: [alice, bob]\ncustom: https://example.com/donate\npatreon:\n"
    parsed = parse_funding_yml(text)
    assert parsed["github"] == ["alice", "bob"]
    assert parsed["custom"] == "https://example.com/donate"
    assert "patreon" not in parsed  # empty value dropped


def test_parse_non_mapping_is_empty():
    assert parse_funding_yml("- just\n- a\n- list\n") == {}
    assert parse_funding_yml(": : bad yaml :::\n") == {}


def test_github_logins_from_scalar_and_list():
    assert funding_yml_github_logins({"github": "Alice"}) == ["alice"]
    assert funding_yml_github_logins({"github": ["Alice", "bob"]}) == ["alice", "bob"]
    assert funding_yml_github_logins({"patreon": "x"}) == []
