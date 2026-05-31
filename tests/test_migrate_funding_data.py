import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "migrate_funding_data", Path("scripts/migrate-funding-data.py"))
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def test_split_rows():
    rows = [{
        "repo": "owner/repo", "repo_id": "42", "github_sponsors": "5",
        "has_funding_yml": "True", "funding_yml_platforms": "github,patreon",
        "has_funding_json": "False", "funding_5y": "", "funding_sources": "2",
        "funding_class": "C", "fetched_at": "2026-05-19T11:10:36+00:00",
    }]
    sponsors, yml = mig.split_rows(rows)
    assert sponsors[0] == {
        "repo": "owner/repo", "repo_id": "42", "github_sponsors": "5",
        "sponsors_status": "ok", "fetched_at": "2026-05-19T11:10:36+00:00"}
    assert yml[0] == {
        "repo": "owner/repo", "repo_id": "42", "has_funding_yml": "True",
        "funding_yml_platforms": "github,patreon", "funding_yml_github": "",
        "fetched_at": "2026-05-19T11:10:36+00:00"}
