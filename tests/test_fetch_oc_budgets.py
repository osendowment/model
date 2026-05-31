"""Tests for src/opencollective/fetch_budgets.py response parsing."""

from src.opencollective.fetch_budgets import YEARS, parse_oc_account


def test_parse_ok_with_per_year_amounts():
    account = {
        "slug": "vuejs", "name": "vue", "currency": "USD",
        "stats": {f"y{y}": {"valueInCents": (y - 2000) * 100000, "currency": "USD"}
                  for y in YEARS},
    }
    row = parse_oc_account("vuejs", account)
    assert row["oc_status"] == "ok"
    assert row["currency"] == "USD"
    assert row["raised_2021"] == "21000"   # (2021-2000)*100000 cents / 100
    assert row["raised_2025"] == "25000"


def test_parse_not_found():
    row = parse_oc_account("ghost", None)
    assert row["oc_status"] == "not_found"
    assert row["raised_2024"] == ""
    assert row["currency"] == ""


def test_parse_missing_year_is_blank():
    account = {"slug": "x", "currency": "EUR",
               "stats": {"y2024": {"valueInCents": 5000, "currency": "EUR"}}}
    row = parse_oc_account("x", account)
    assert row["raised_2024"] == "50"
    assert row["raised_2021"] == ""        # absent alias → blank
    assert row["currency"] == "EUR"
