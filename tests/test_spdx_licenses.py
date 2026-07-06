"""Tests for the SPDX list fetcher and the unified OSS-set builder."""

from src.sources.osi import fetch_licenses as osi
from src.sources.spdx import fetch_licenses as spdx

_JSON = {"licenses": [
    {"licenseId": "MIT", "name": "MIT License", "isOsiApproved": True,
     "isFsfLibre": True, "reference": "https://spdx.org/licenses/MIT.html",
     "seeAlso": ["https://opensource.org/license/mit/"]},
    {"licenseId": "x11", "name": "X11 License", "isOsiApproved": False,
     "isFsfLibre": True, "reference": ""},
    {"licenseId": "CC-BY-4.0", "name": "CC Attribution 4.0",
     "isOsiApproved": False, "isFsfLibre": True, "reference": ""},
    {"licenseId": "Proprietary-ish", "name": "Nope",
     "isOsiApproved": False, "reference": ""},
    {"licenseId": "curl", "name": "curl License",
     "isOsiApproved": False, "isFsfLibre": False, "reference": ""},
]}


def _spdx_rows():
    return spdx.build_rows(_JSON, now="2026-07-06T00:00:00Z")


def test_spdx_build_rows_stores_full_list_with_both_flags():
    rows = {r["spdx_id"]: r for r in _spdx_rows()}
    assert len(rows) == 5                       # unfiltered — policy lives downstream
    assert rows["mit"]["is_osi_approved"] is True
    assert rows["mit"]["is_fsf_libre"] is True
    assert rows["mit"]["osi_url"] == "https://opensource.org/license/mit/"
    assert rows["x11"]["is_osi_approved"] is False
    assert rows["x11"]["is_fsf_libre"] is True
    assert rows["proprietary-ish"]["is_fsf_libre"] is False   # absent → False


def _as_csv_strings(rows):
    # the builder reads the CSV back, where booleans are strings
    return [{k: str(v) for k, v in r.items()} for r in rows]


def test_unified_set_is_osi_union_fsf_software_union_extras():
    rows = osi.build_rows(_as_csv_strings(_spdx_rows()))
    by_id = {r["spdx_id"]: r for r in rows}
    assert by_id["mit"]["source"] == "osi"        # OSI wins the label over FSF
    assert by_id["x11"]["source"] == "fsf"        # FSF-only software → admitted
    assert "cc-by-4.0" not in by_id               # FSF-libre content → excluded
    assert "proprietary-ish" not in by_id         # neither body → excluded
    assert by_id["curl"]["source"] == "extras"    # hand-curated remainder


def test_compare_splits_fsf_only_into_software_and_content():
    c = osi.compare(_as_csv_strings(_spdx_rows()))
    assert c["both"] == {"mit"}
    assert c["fsf_only_software"] == {"x11"}
    assert c["fsf_only_content"] == {"cc-by-4.0"}
    assert c["osi_only"] == set()


def test_content_prefixes_cover_the_known_families():
    for sid in ("cc-by-4.0", "cc-by-sa-4.0", "cc0-1.0", "gfdl-1.3-only",
                "ofl-1.0", "odbl-1.0"):
        assert osi._is_content_license(sid), sid
    for sid in ("x11", "openssl", "ruby", "wtfpl", "sgi-b-2.0"):
        assert not osi._is_content_license(sid), sid


def test_redundant_extras_entry_warns_and_keeps_body_source(caplog):
    import logging
    rows_json = {"licenses": [
        {"licenseId": "curl", "name": "curl License",
         "isOsiApproved": False, "isFsfLibre": True, "reference": ""},
    ]}
    raw = _as_csv_strings(spdx.build_rows(rows_json, now="2026-07-06T00:00:00Z"))
    with caplog.at_level(logging.WARNING):
        rows = osi.build_rows(raw)
    by_id = {r["spdx_id"]: r for r in rows}
    # admitted once, via the body that lists it — not double-counted as extras
    assert by_id["curl"]["source"] == "fsf"
    assert any("remove it from EXTRAS" in m for m in caplog.messages)
