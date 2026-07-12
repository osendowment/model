"""Tests for src/sources/openssf/extract_checks.py — repo_id stamping.

The workload consumer (`build_workload._load_openssf_maintained`) joins
checks.csv on the stable numeric `repo_id`, not the drifting slug. The
generator must therefore stamp `repo_id` on every row, or a re-run silently
zeros `openssf_maintained` for every repo.
"""

import csv
import json


def test_extract_checks_stamps_repo_id_with_rename_and_value_overlay(tmp_path, monkeypatch):
    """Every checks.csv row must carry a non-empty, rename-proof repo_id.

    Regression: extract_checks wrote no repo_id column at all; the on-disk
    file only kept one inherited from an older generator, so re-running the
    script blanked repo_id for every row and broke the id-join downstream.

    Proves both halves of the id map:
      - `old/name` is present in data.json under its OLD slug; repos.csv keys
        both the current slug and the rename-resolved full_name to the same
        id, so it still resolves.
      - `v/only` is known only to value.csv (repos.csv hasn't caught up) and
        must resolve through the value-id overlay.
    """
    from src.sources.openssf import extract_checks

    data = {
        "o/n": {"score": 7, "date": "2026-01-01",
                "checks": [{"name": "Maintained", "score": 5}]},
        "old/name": {"score": 8, "date": "2026-01-02",
                     "checks": [{"name": "Maintained", "score": 6}]},
        "v/only": {"score": 9, "date": "2026-01-03",
                   "checks": [{"name": "Maintained", "score": 7}]},
    }
    input_file = tmp_path / "data.json"
    input_file.write_text(json.dumps(data), encoding="utf-8")
    output_file = tmp_path / "checks.csv"

    monkeypatch.setattr(extract_checks, "INPUT_FILE", input_file)
    monkeypatch.setattr(extract_checks, "OUTPUT_FILE", output_file)
    # repos.csv keys both the current slug and the rename-resolved full_name to
    # the same id, so the stale "old/name" still resolves to gh/2.
    monkeypatch.setattr(extract_checks, "load_repo_ids",
                        lambda: {"o/n": "gh/1", "new/name": "gh/2", "old/name": "gh/2"})
    # "v/only" lives only in value.csv — the overlay must still resolve it.
    monkeypatch.setattr(extract_checks, "load_value_repo_ids",
                        lambda: {"v/only": "gh/3"})

    extract_checks.main()

    with open(output_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames is not None
        assert "repo_id" in reader.fieldnames, "repo_id must be in the CSV header"
        rows = {r["repo"]: r for r in reader}

    expected = {"o/n": "gh/1", "old/name": "gh/2", "v/only": "gh/3"}
    for slug, rid in expected.items():
        assert rows[slug]["repo_id"] == rid, f"{slug} should map to {rid}"
    # Every row carries a non-empty id — no silent blanks.
    assert all(r["repo_id"] for r in rows.values())
