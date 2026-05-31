"""Tests for src/opencollective/fetch_budgets.py response parsing."""

from src.opencollective.fetch_budgets import YEARS, parse_oc_account, _is_fresh


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


def test_error_rows_are_never_fresh():
    # a recent timestamp must not shield an errored row from a retry
    recent = "2999-01-01T00:00:00+00:00"
    assert _is_fresh({"oc_status": "error", "fetched_at": recent}, 30) is False
    assert _is_fresh({"oc_status": "ok", "fetched_at": recent}, 30) is True


def test_parse_missing_year_is_blank():
    account = {"slug": "x", "currency": "EUR",
               "stats": {"y2024": {"valueInCents": 5000, "currency": "EUR"}}}
    row = parse_oc_account("x", account)
    assert row["raised_2024"] == "50"
    assert row["raised_2021"] == ""        # absent alias → blank
    assert row["currency"] == "EUR"
