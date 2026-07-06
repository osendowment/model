"""Tests for src/eligibility/build_funding.py — join, info cols, funding score."""

from dataclasses import dataclass

import pytest

from src.eligibility import build_funding as bf
from src.eligibility.build_funding import (
    _declares_funding_channel,
    _intent_flag,
    _load_maintainer_fundable,
    _nonprofit_flag,
    _propagate_owner_intent,
)


@pytest.fixture(autouse=True)
def _isolate_bf_sources(monkeypatch):
    """Keep every build() test off the real contributor-commits / maintainer-
    sponsors files. A test that exercises the bus-factor-maintainer signal
    overrides these two explicitly."""
    monkeypatch.setattr(bf, "load_bf_contributors", lambda *a, **k: {})
    monkeypatch.setattr(bf, "_load_maintainer_fundable", lambda *a, **k: set())


def _row(**kw):
    base = {
        "gh_sponsorships_in": "", "gh_sponsors_enabled": "False",
        "has_funding_links": "False", "has_funding_yml": "False", "has_funding_json": "False",
        "has_npm_funding": "False", "has_pypi_funding": "False",
        "bf_maintainer_fundable": "False",
        "oc_slug": "", "host": "", "owner": "",
    }
    base.update(kw)
    return base


def test_intent_false_when_no_signal():
    assert _intent_flag(_row()) is False


def test_intent_each_single_signal_is_true():
    assert _intent_flag(_row(gh_sponsors_enabled="True")) is True   # Sponsors enabled → intent
    assert _intent_flag(_row(has_funding_links="True")) is True
    assert _intent_flag(_row(has_funding_yml="True")) is True       # FUNDING.yml file exists (even if empty)
    assert _intent_flag(_row(has_funding_json="True")) is True      # repo OR owner in FLOSS Fund
    assert _intent_flag(_row(has_npm_funding="True")) is True
    assert _intent_flag(_row(has_pypi_funding="True")) is True
    assert _intent_flag(_row(bf_maintainer_fundable="True")) is True  # a BF maintainer has Sponsors
    assert _intent_flag(_row(oc_slug="babel")) is True
    assert _intent_flag(_row(paypal="https://www.paypal.com/paypalme/X")) is True
    assert _intent_flag(_row(host="apache")) is True
    assert _intent_flag(_row(owner="meta.com")) is True


def test_intent_sponsor_count_alone_is_not_intent():
    # The inbound count is not a separate signal — any count implies the listing
    # is enabled, so gh_sponsors_enabled covers it. Count alone (without the
    # enabled flag) is never intent.
    assert _intent_flag(_row(gh_sponsorships_in="3")) is False
    assert _intent_flag(_row(gh_sponsorships_in="0")) is False


def test_declares_funding_channel():
    # a self-declared channel (Sponsors / FUNDING.yml / FLOSS / registry / OC)…
    assert _declares_funding_channel(_row(has_funding_yml="True")) is True
    assert _declares_funding_channel(_row(gh_sponsors_enabled="True")) is True
    assert _declares_funding_channel(_row(oc_slug="rustcrypto")) is True
    # …but an institutional host/owner is NOT a self-declared channel (it's the
    # foundation/company backing, propagated via overrides, not the maintainer).
    assert _declares_funding_channel(_row(host="apache")) is False
    assert _declares_funding_channel(_row(owner="meta.com")) is False
    assert _declares_funding_channel(_row()) is False


def test_propagate_owner_intent_spreads_across_siblings():
    # serde-rs/serde declares a channel; serde-rs/json + bytes declare nothing.
    rows = [
        {"repo": "serde-rs/serde", "has_funding_yml": "True", "intent": "True"},
        {"repo": "serde-rs/json", "intent": "False"},
        {"repo": "serde-rs/bytes", "intent": "False"},
        {"repo": "lonely/repo", "intent": "False"},   # owner has no channel anywhere
    ]
    _propagate_owner_intent(rows)
    byrepo = {r["repo"]: r for r in rows}
    assert byrepo["serde-rs/json"]["intent"] == "True"    # inherited from sibling serde
    assert byrepo["serde-rs/bytes"]["intent"] == "True"
    assert byrepo["lonely/repo"]["intent"] == "False"     # no owner channel → unchanged


def test_build_funding_owner_intent_propagates_but_not_nonprofit(monkeypatch):
    """One repo's FUNDING.yml makes every sibling the owner has intent=True, yet a
    company owner stays nonprofit=False (propagation only adds intent).

    Owner `serde` declares a channel on serde/serde (FUNDING.yml) → serde/json
    inherits intent. Owner `corp` is company-owned via an override AND declares a
    channel on corp/a → corp/b inherits intent but both stay nonprofit=False, so
    the company org gains intent without becoming eligible. `lonely/x` (no channel
    anywhere in its org) stays intent=False.
    """
    def rows_by_repo(p):
        s = str(p)
        if "funding-yml.csv" in s:
            return {"serde/serde": {"has_funding_links": "True", "funding_link_platforms": "github"},
                    "corp/a": {"has_funding_links": "True", "funding_link_platforms": "github"}}
        if "sponsors.csv" in s:
            return {"rich/r": {"gh_sponsorships_in": "100"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos", lambda **kw: [
        E("serde/serde"), E("serde/json"),
        E("corp/a"), E("corp/b"), E("lonely/x"), E("rich/r")])
    monkeypatch.setattr(bf, "load_rows_by_id", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000", "oc_status": "ok"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: (
        {}, {"corp": {"owner": "bigco.com", "owner_type": "company"}}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["serde/json"]["intent"] == "True"      # inherited from serde/serde
    assert rows["serde/json"]["nonprofit"] == "True"
    assert rows["corp/b"]["intent"] == "True"          # inherited from corp/a
    assert rows["corp/b"]["nonprofit"] == "False"      # company owner → still ineligible
    assert rows["lonely/x"]["intent"] == "False"       # no channel in its org


def test_load_maintainer_fundable_unions_curated_override(tmp_path):
    """The fetched Sponsors-listing set is UNIONED with the curated
    maintainer-overrides.csv (login,reason) — a maintainer who solicits via a
    channel the fetch can't see (external profile funding link) is fundable even
    with has_sponsors_listing=False."""
    sponsors = tmp_path / "maintainer-sponsors.csv"
    sponsors.write_text(
        "user_id,login,has_sponsors_listing,status,fetched_at\n"
        "1,marijnh,True,ok,2026-07-05\n"          # real Sponsors listing → fundable
        "2,hartwork,False,ok,2026-07-05\n"        # no listing (solicits via FSFE link)
        "3,nobody,False,ok,2026-07-05\n",         # genuinely not fundable
        encoding="utf-8")
    overrides = tmp_path / "maintainer-overrides.csv"
    overrides.write_text("login,reason\nhartwork,external FSFE funding link\n",
                         encoding="utf-8")
    # call the imported name directly (the autouse fixture only mocks bf.<attr>)
    # without the override, only the real listing counts
    assert _load_maintainer_fundable(sponsors) == {"marijnh"}
    # with it, the curated login is unioned in (case-insensitive), nobody stays out
    assert _load_maintainer_fundable(sponsors, overrides) == {"marijnh", "hartwork"}


def test_nonprofit_default_true():
    assert _nonprofit_flag("", "") is True
    assert _nonprofit_flag("nonprofit", "") is True


def test_nonprofit_false_when_corporate():
    assert _nonprofit_flag("", "company") is False
    assert _nonprofit_flag("company", "") is False
    assert _nonprofit_flag("Company", "nonprofit") is False  # case-insensitive


@dataclass
class E:
    repo: str
    repo_id: str = ""
    value_class: str = "B"
    git_url: str = ""

    def __post_init__(self):
        # default repo_id to the slug so id-keyed source mocks (keyed by name) match
        if not self.repo_id:
            self.repo_id = self.repo


def test_oc_avg_funding_zero_default():
    assert bf.oc_avg_funding("", {}) == "0"                       # no slug → $0
    assert bf.oc_avg_funding("ghost", {"x": {"raised_2024": "5"}}) == "0"
    assert bf.oc_avg_funding("x", {"x": {"raised_2024": "10", "raised_2025": "20"}}) == "15"


def test_assemble_row_stars_forks_sponsorships():
    row = bf.assemble_row(
        repo="o/r", repo_id="1",
        sponsors={"gh_sponsorships_in": "12"},
        yml={"has_funding_links": "True", "funding_link_platforms": "github"},
        export={}, host="", host_type="", owner="", owner_type="",
        repo_meta={"stars": "5000", "forks": "300"},
        sponsoring_count="39",
    )
    assert row["has_funding_links"] == "True"
    assert row["has_funding_json"] == "False"  # no FLOSS manifest (repo or owner)
    assert row["gh_sponsorships_in"] == "12"          # inbound (owner only)
    assert "gh_sponsors_out" not in row        # outbound folded into gh_sponsorships
    assert row["gh_sponsorships"] == "51"      # in + out (out from sponsoring_count)
    assert row["gh_stars"] == "5000"           # info column
    assert row["gh_forks"] == "300"            # info column
    assert row["oc_avg_funding"] == "0"        # no OC attributed → $0
    assert row["host_score"] == "1"            # no backing → ×1
    assert "score" not in row                  # filled by build()


def test_assemble_row_paypal_is_intent_channel_not_corporate():
    """A curated PayPal.me handle is a declared funding channel: it sets intent,
    counts toward channels_count, and — being a personal donation link — leaves
    the repo nonprofit (it is not corporate backing)."""
    row = bf.assemble_row(
        repo="ronaldoussoren/pyobjc", repo_id="243933900",
        sponsors={}, yml={}, export={},
        host="", host_type="", owner="", owner_type="",
        repo_meta={},
        paypal="https://www.paypal.com/paypalme/RonaldOussoren",
    )
    assert row["paypal"] == "https://www.paypal.com/paypalme/RonaldOussoren"
    assert row["intent"] == "True"             # declared channel → intent
    assert row["channels_count"] == "1"        # paypal counted as a channel
    assert row["nonprofit"] == "True"          # personal donation link ≠ corporate


def _base_mocks(monkeypatch, repos):
    """Shared setup: rich/r is funded (a repo-level OC worth $10k) so the two
    percentile axes vary. rich/r is in its OWN org so org-level attribution never
    credits the other (deliberately unfunded) test repos.
    """
    def rows_by_repo(p):
        if "sponsors.csv" in str(p):
            return {"rich/r": {"gh_sponsorships_in": "100"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos", lambda **kw: repos)
    monkeypatch.setattr(bf, "load_rows_by_id", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({}, {}))
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000", "oc_status": "ok"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))


def test_build_funding_score_lower_funding_higher_score(monkeypatch):
    _base_mocks(monkeypatch, [E("poor/p"), E("rich/r")])
    rows = {r["repo"]: r for r in bf.build()}
    # poor/p: 0 sponsors + $0 OC → worst on both axes → score 100
    assert rows["poor/p"]["score"] == 100
    assert int(rows["rich/r"]["score"]) < int(rows["poor/p"]["score"])


def test_build_funding_declared_registry_channel_caps_score(monkeypatch):
    """A repo with no measured funding but a DECLARED registry funding channel —
    npm package.json `funding` OR PyPI `project_urls` — is capped at
    DECLARED_FUNDING_CAP (79) instead of the 100 max-risk plateau; the channel is
    counted. An identical repo with no declared channel stays at 100."""
    def rows_by_repo(p):
        if "sponsors.csv" in str(p):
            return {"rich/r": {"gh_sponsorships_in": "100"}}
        if "npm/funding.csv" in str(p):
            return {"npm/d": {"has_npm_funding": "True",
                              "npm_funding_url": "https://github.com/sponsors/x"}}
        if "pypi/funding.csv" in str(p):
            return {"pypi/d": {"has_pypi_funding": "True",
                               "pypi_funding_platforms": "github"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos",
                        lambda **kw: [E("plain/p"), E("npm/d"), E("pypi/d"), E("rich/r")])
    monkeypatch.setattr(bf, "load_rows_by_id", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({}, {}))
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000", "oc_status": "ok"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100                  # unfunded, no channel
    for d in ("npm/d", "pypi/d"):
        assert rows[d]["channels_count"] == "1"             # registry channel counted
        assert rows[d]["score"] == bf.DECLARED_FUNDING_CAP  # 100 → 79


def test_build_funding_paypal_override_caps_score_and_sets_intent(monkeypatch):
    """A curated PayPal.me override (keyed by repo_id in by_id) on an otherwise-
    unfunded repo flips intent to True, counts a channel, caps the score at
    DECLARED_FUNDING_CAP, and leaves the repo nonprofit. An identical repo with
    no override stays at 100/intent False. (Mirrors ronaldoussoren/pyobjc.)"""
    _base_mocks(monkeypatch, [E("plain/p"), E("pp/d"), E("rich/r")])
    # override keyed by the repo's stable id (E defaults repo_id to the slug)
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: (
        {"pp/d": {"host": "", "host_type": "", "owner": "", "owner_type": "",
                  "oc_slug": "", "paypal": "https://www.paypal.com/paypalme/X"}}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100                  # unfunded, no channel
    assert rows["plain/p"]["intent"] == "False"
    assert rows["pp/d"]["paypal"] == "https://www.paypal.com/paypalme/X"
    assert rows["pp/d"]["channels_count"] == "1"            # paypal counted
    assert rows["pp/d"]["score"] == bf.DECLARED_FUNDING_CAP  # 100 → 79
    assert rows["pp/d"]["intent"] == "True"
    assert rows["pp/d"]["nonprofit"] == "True"              # not corporate


def test_build_funding_scraped_foundation_host_lowers_score(monkeypatch):
    """A scraped FOSS-foundation host (no override) defaults to nonprofit.

    plain/p and found/f are both unfunded on the two scored axes (0 sponsors,
    $0 OC) → both percentiles 100. found/f has a scraped foundation host, so its
    host_type defaults to nonprofit (host_score 0.5) and the backing enters the
    geom mean as a third axis at 50: round(∛(100·100·50)) = 79 (< the 100 plateau).
    """
    _base_mocks(monkeypatch, [E("plain/p"), E("found/f"), E("rich/r")])
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {"found/f": "apache"})

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100           # unfunded, no backing
    assert rows["found/f"]["host"] == "apache"
    assert rows["found/f"]["host_type"] == "nonprofit"
    assert rows["found/f"]["host_score"] == "0.5"
    assert rows["found/f"]["score"] == 79            # ∛(100·100·50) nonprofit host


def test_build_funding_company_owner_override_zeros_risk(monkeypatch):
    """A company host/owner override fully resources the repo → score 1.

    The combined host_score = min(nonprofit host 0.5, company owner 0) = 0
    collapses the funding risk to the floor (max(1, …) = 1) — the most-funded
    backer (the company) wins.
    """
    _base_mocks(monkeypatch, [E("plain/p"), E("corp/c", "77"), E("rich/r")])
    # overrides are keyed by the stable repo_id (corp/c → 77)
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({
        "77": {"host": "x.foundation", "host_type": "nonprofit",
               "owner": "bigco.com", "owner_type": "company"}}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100
    assert rows["corp/c"]["owner_type"] == "company"
    assert rows["corp/c"]["host_score"] == "0"       # min(0.5, 0) = 0
    assert rows["corp/c"]["score"] == 1              # company backing → floor


def test_load_funding_overrides_splits_id_and_org(tmp_path):
    """An ``owner/*`` row → org map (keyed by owner); a plain ``owner/name`` row →
    the per-repo map keyed by its stable `repo_id` (not the slug)."""
    p = tmp_path / "overrides.csv"
    p.write_text(
        "repo,repo_id,host,host_type,gh_user,owner,owner_type,oc_slug,paypal\n"
        "boto/*,,,,boto,amazon.com,company,,\n"
        "rust-lang/rust,724712,rustfoundation.org,nonprofit,rust-lang,,,,\n"
        "ronaldoussoren/pyobjc,243933900,,,ronaldoussoren,,,,https://www.paypal.com/paypalme/RonaldOussoren\n",
        encoding="utf-8")
    by_id, by_org = bf._load_funding_overrides(p)
    assert "boto" in by_org and by_org["boto"]["owner"] == "amazon.com"
    assert "boto/*" not in by_id              # the glob is not a per-repo key
    assert by_id["724712"]["host"] == "rustfoundation.org"   # keyed by repo_id
    assert by_id["243933900"]["paypal"] == "https://www.paypal.com/paypalme/RonaldOussoren"


def test_build_funding_org_level_override_applies_with_per_repo_win(monkeypatch):
    """An ``owner/*`` override backs every repo under the owner, but a per-repo
    row for one of them wins.

    org `corp` is company-owned via `corp/* → bigco.com`. `corp/a` and `corp/b`
    both inherit it (host_score 0 → score floors at 1, nonprofit False). `corp/c`
    has its own per-repo row (a nonprofit foundation host, no company owner) which
    must override the org rule → host_score 0.5, score 79, nonprofit True.
    """
    _base_mocks(monkeypatch, [E("corp/a", "1"), E("corp/b", "2"), E("corp/c", "3"),
                              E("indie/x"), E("rich/r")])
    # per-repo override keyed by repo_id (corp/c → 3); org rule keyed by owner name
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: (
        {"3": {"host": "x.foundation", "host_type": "nonprofit",
               "owner": "", "owner_type": ""}},
        {"corp": {"host": "", "host_type": "",
                  "owner": "bigco.com", "owner_type": "company"}}))

    rows = {r["repo"]: r for r in bf.build()}
    for repo in ("corp/a", "corp/b"):
        assert rows[repo]["owner"] == "bigco.com"      # inherited from corp/*
        assert rows[repo]["owner_type"] == "company"
        assert rows[repo]["nonprofit"] == "False"
        assert rows[repo]["score"] == 1                # company backing → floor
    assert rows["corp/c"]["owner"] == ""               # per-repo row wins
    assert rows["corp/c"]["host"] == "x.foundation"
    assert rows["corp/c"]["nonprofit"] == "True"
    assert rows["corp/c"]["score"] == 79               # nonprofit host only
    assert rows["indie/x"]["owner"] == ""              # other org untouched
    assert rows["indie/x"]["intent"] == "False"


def test_build_funding_oc_repo_full_vs_org_split(monkeypatch):
    """OC attribution rule:

    - a **repo-level** collective pays that repo its FULL avg budget;
    - an **org-level** collective splits equally across the org's class-A repos
      (so a class-B org-mate gets $0 unless it has its own collective).
    """
    # org `aio`: a1 & a2 are class A, b is class B; the org collective is $9000.
    # solo/repo has its own repo-level collective ($500).
    repos = [E("aio/a1", value_class="A"), E("aio/a2", value_class="A"),
             E("aio/b"), E("solo/repo", value_class="A")]
    monkeypatch.setattr(bf, "load_top_repos", lambda **kw: repos)
    monkeypatch.setattr(bf, "load_rows_by_id", lambda p: {})
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({}, {}))
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {
        "aio-libs": {"raised_2024": "9000", "oc_status": "ok"},
        "solo": {"raised_2024": "500", "oc_status": "ok"}})
    monkeypatch.setattr(bf, "_load_oc_index",
                        lambda *a, **k: ({"solo/repo": "solo"}, {"aio": "aio-libs"}))

    rows = {r["repo"]: r for r in bf.build()}
    # org-level $9000 split across 2 class-A repos → $4500 each
    assert rows["aio/a1"]["oc_avg_funding"] == "4500"
    assert rows["aio/a1"]["oc_slug"] == "aio-libs"
    assert rows["aio/a2"]["oc_avg_funding"] == "4500"
    assert rows["aio/b"]["oc_avg_funding"] == "0"    # class B → no org share
    # repo-level collective → full budget to the named repo
    assert rows["solo/repo"]["oc_avg_funding"] == "500"
    assert rows["solo/repo"]["oc_slug"] == "solo"


def test_build_funding_real_zero_oc_counts_as_intent(monkeypatch):
    """A real OC channel (`oc_status == "ok"`) signals sustainability intent even
    at $0 raised — its slug is attributed (with oc_avg_funding $0). A `not_found`
    slug is not a real channel and is never attributed."""
    repos = [E("live/zero", value_class="A"), E("dead/none", value_class="A")]
    monkeypatch.setattr(bf, "load_top_repos", lambda **kw: repos)
    monkeypatch.setattr(bf, "load_rows_by_id", lambda p: {})
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({}, {}))
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {
        "zero": {"raised_2024": "0", "oc_status": "ok"},   # real collective, $0 raised
        "ghost": {"oc_status": "not_found"}})              # fetched, doesn't exist
    monkeypatch.setattr(bf, "_load_oc_index",
                        lambda *a, **k: ({"live/zero": "zero", "dead/none": "ghost"}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["live/zero"]["oc_slug"] == "zero"          # real $0 OC → attributed
    assert rows["live/zero"]["oc_avg_funding"] == "0"
    assert rows["live/zero"]["intent"] == "True"           # real channel → intent
    assert rows["dead/none"]["oc_slug"] == ""              # not_found → not attributed
    assert rows["dead/none"]["intent"] == "False"


def test_build_funding_override_oc_slug_authoritative(monkeypatch):
    """A curated override row is authoritative for OC, overriding the auto-map.

    - empty `oc_slug` on a curated repo → NO OC (suppresses a spurious match);
    - a non-empty `oc_slug` is used even if it differs from the reverse-map.
    """
    repos = [E("big/junkmatch", "10", value_class="A"), E("big/keep", "11", value_class="A")]
    monkeypatch.setattr(bf, "load_top_repos", lambda **kw: repos)
    monkeypatch.setattr(bf, "load_rows_by_id", lambda p: {})
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {
        "junk": {"raised_2024": "9999"}, "real": {"raised_2024": "200"}})
    # reverse-map would auto-match org `big` to the junk collective…
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({}, {"big": "junk"}))
    # …but overrides win (keyed by repo_id): junkmatch(10) → empty, keep(11) → slug.
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({
        "10": {"oc_slug": ""},
        "11": {"oc_slug": "real"}}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["big/junkmatch"]["oc_slug"] == ""        # empty override → suppressed
    assert rows["big/junkmatch"]["oc_avg_funding"] == "0"
    assert rows["big/keep"]["oc_slug"] == "real"         # explicit override slug
    assert rows["big/keep"]["oc_avg_funding"] == "200"


def test_build_funding_unmeasured_funding_link_caps_score(monkeypatch):
    """A repo declaring a funding LINK we can't value (liberapay, ko_fi, …) is
    capped at 79 even with $0 measured — e.g. tukaani-project/xz's Liberapay. But
    a link to a MEASURED platform (GitHub Sponsors / Open Collective) does NOT cap
    on its own, because the score already reflects the real sponsor/budget signal.
    """
    def rows_by_repo(p):
        s = str(p)
        if "funding-yml.csv" in s:
            return {"link/lp": {"has_funding_links": "True", "funding_link_platforms": "liberapay"},
                    "gh/only": {"has_funding_links": "True", "funding_link_platforms": "github"}}
        if "sponsors.csv" in s:
            return {"rich/r": {"gh_sponsorships_in": "100"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos",
                        lambda **kw: [E("link/lp"), E("gh/only"), E("plain/p"), E("rich/r")])
    monkeypatch.setattr(bf, "load_rows_by_id", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_id", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: ({}, {}))
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: ({}, {}, {}))
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000", "oc_status": "ok"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["link/lp"]["score"] == bf.DECLARED_FUNDING_CAP  # liberapay → capped 79
    assert rows["gh/only"]["score"] == 100   # github-only is measured → no cap, 0 sponsors
    assert rows["plain/p"]["score"] == 100   # no channel at all


def test_export_by_repo_splits_id_and_canonical_slug(tmp_path, monkeypatch):
    """funding-json.csv rows with a repo_id are keyed by id; blank-id rows fall
    back to the slug map, canonicalized (an old pre-rename slug resolves to the
    canonical name `load_top_repos` uses)."""
    p = tmp_path / "funding-json.csv"
    p.write_text(
        "id,project_repository,project_repository_resolved,channel_platforms,repo_id\n"
        "1,https://github.com/old-org/lib,,liberapay,424242\n"
        "2,https://github.com/gozala/events,,ko_fi,\n",
        encoding="utf-8")
    # the blank-id row's slug predates a rename → canonical map resolves it
    monkeypatch.setattr(bf, "canonical_repo_map",
                        lambda: {"gozala/events": "browserify/events"})
    by_id, by_slug, _by_url = bf._export_by_repo(p)
    assert by_id["424242"]["channel_platforms"] == "liberapay"
    assert "old-org/lib" not in by_slug            # id-carrying row is id-keyed only
    assert by_slug["browserify/events"]["channel_platforms"] == "ko_fi"
    assert "gozala/events" not in by_slug          # keyed canonical, not raw


def test_build_funding_floss_manifest_by_repo_id_survives_rename(monkeypatch):
    """A manifest recorded under a repo's OLD slug still reaches the renamed
    entry: the row carries the stable repo_id and build() joins by id first.
    A blank-id manifest row still matches by canonical slug (fallback)."""
    _base_mocks(monkeypatch, [E("new-org/lib", "424242"), E("slugonly/tool", "9"),
                              E("plain/p"), E("rich/r")])
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: (
        # id-keyed: manifest URL still names the pre-rename slug old-org/lib,
        # but the fetcher stamped the stable id 424242 (= new-org/lib's id).
        {"424242": {"channel_platforms": "liberapay"}},
        # blank-id row: only reachable via the canonical slug fallback.
        {"slugonly/tool": {"channel_platforms": "ko_fi"}},
        {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["new-org/lib"]["has_funding_json"] == "True"    # found by repo_id
    assert rows["new-org/lib"]["channels_count"] == "1"
    assert rows["new-org/lib"]["score"] == bf.DECLARED_FUNDING_CAP
    assert rows["slugonly/tool"]["has_funding_json"] == "True"  # found by slug
    assert rows["slugonly/tool"]["channels_count"] == "1"
    assert rows["plain/p"]["has_funding_json"] == "False"       # neither map → no signal


def test_build_funding_org_manifest_caps_score_and_counts_channel(monkeypatch):
    """A fundable ORG (FLOSS manifest registered for the whole GitHub org) marks
    every repo it owns: has_funding_json=True (owner-level), its channels count,
    and the score is capped at DECLARED_FUNDING_CAP even with $0 measured. A repo
    in a different, non-fundable org stays at the 100 plateau.
    """
    _base_mocks(monkeypatch, [E("zulip/zulip"), E("zulip/zulip-mobile"),
                              E("plain/p"), E("rich/r")])
    # org `zulip` is fundable via an org-page manifest declaring liberapay+github.
    monkeypatch.setattr(bf, "_fundable_orgs",
                        lambda p: {"zulip": {"channel_platforms": "liberapay,github"}})

    rows = {r["repo"]: r for r in bf.build()}
    for repo in ("zulip/zulip", "zulip/zulip-mobile"):
        assert rows[repo]["has_funding_json"] == "True"      # owner-level FLOSS → all org repos
        assert rows[repo]["channels_count"] == "2"           # org channels counted
        assert rows[repo]["score"] == bf.DECLARED_FUNDING_CAP  # 100 → 79 (cap)
    assert rows["plain/p"]["has_funding_json"] == "False"
    assert rows["plain/p"]["score"] == 100                   # different org → no cap


def test_build_funding_bf_maintainer_fundable_flips_intent(monkeypatch):
    """A repo with NO other funding signal gains intent when a bus-factor
    maintainer is personally fundable; a repo whose fundable contributor is NOT
    in its bus-factor set stays intent=False (the signal only fires for people
    who carry the repo)."""
    _base_mocks(monkeypatch, [E("solo/lib", repo_id="gh/1"),
                              E("drive/by", repo_id="gh/2"),
                              E("rich/r", repo_id="gh/9")])
    # solo/lib: its BF maintainer `maint` is fundable → intent.
    # drive/by: `maint` also appears but is NOT in its BF set → no intent.
    monkeypatch.setattr(bf, "load_bf_contributors",
                        lambda *a, **k: {"gh/1": ["maint"], "gh/2": ["nobody"]})
    monkeypatch.setattr(bf, "_load_maintainer_fundable", lambda *a, **k: {"maint"})

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["solo/lib"]["bf_maintainer_fundable"] == "True"
    assert rows["solo/lib"]["intent"] == "True"
    assert rows["drive/by"]["bf_maintainer_fundable"] == "False"
    assert rows["drive/by"]["intent"] == "False"


def test_load_maintainer_fundable_filters_status_and_flag(tmp_path):
    """Only rows that resolved (status=ok) with an active listing count; a
    network error or a False listing is excluded; logins are lowercased."""
    p = tmp_path / "maintainer-sponsors.csv"
    p.write_text(
        "user_id,login,has_sponsors_listing,status,fetched_at\n"
        "1,MarijnH,True,ok,2026-07-05T00:00:00+00:00\n"      # fundable → kept (lowercased)
        "2,amanieu,False,ok,2026-07-05T00:00:00+00:00\n"     # resolved, not fundable → excluded
        "3,ghost,True,error,2026-07-05T00:00:00+00:00\n",    # True but failed lookup → excluded
        encoding="utf-8",
    )
    assert _load_maintainer_fundable(p) == {"marijnh"}


def test_load_maintainer_fundable_missing_file(tmp_path):
    assert _load_maintainer_fundable(tmp_path / "absent.csv") == set()


def test_overrides_use_canonical_host_codes():
    """Regression guard for host-key fragmentation: every foundation-backed
    override in overrides.csv must carry the canonical short code the roster
    matcher emits (e.g. `fsf/gnu`, `lf/openjs`, `sfc`, `xorg`, `lf`), NOT the
    raw foundation domain (`fsf.org`, `openjsf.org`, `sfconservancy.org`,
    `x.org`, `linuxfoundation.org`). A raw domain that has a canonical code
    splits one steward across two host keys, breaking host-level grouping.
    Domains with NO canonical code (rustfoundation.org, react.foundation, …)
    are legitimately unique and allowed."""
    import csv
    from src.sources.funding.match_repos import DOMAIN_SUFFIX_HOST

    aliased_domains = {domain for domain, _code in DOMAIN_SUFFIX_HOST}
    offenders = []
    with open(bf.OVERRIDES_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            host = (row.get("host") or "").strip()
            if host in aliased_domains:
                offenders.append((row.get("repo"), host))
    assert not offenders, (
        "overrides.csv uses raw foundation domains that collide with canonical "
        f"host codes (normalize to the host-by-repo.csv code): {offenders}")


# ── GitLab funding checks ─────────────────────────────────────────────────────

def test_gitlab_instance_parsing():
    assert bf._gitlab_instance("gl/12345") == "gitlab.com"
    assert bf._gitlab_instance("gl/salsa.debian.org-9696") == "salsa.debian.org"
    assert bf._gitlab_instance("gl/gitlab.inria.fr-22470") == "gitlab.inria.fr"
    assert bf._gitlab_instance("gh/123") == ""
    assert bf._gitlab_instance("") == ""


def test_load_gitlab_hosts(tmp_path):
    p = tmp_path / "gitlab-hosts.csv"
    p.write_text("gitlab_host,host,host_type,reason\n"
                 "salsa.debian.org,debian.org,nonprofit,debian's gitlab\n"
                 "gitlab.kitware.com,kitware.com,company,kitware's gitlab\n"
                 ",skipped.org,nonprofit,no instance host\n", encoding="utf-8")
    hosts = bf._load_gitlab_hosts(p)
    assert hosts == {"salsa.debian.org": ("debian.org", "nonprofit"),
                     "gitlab.kitware.com": ("kitware.com", "company")}
    assert bf._load_gitlab_hosts(tmp_path / "missing.csv") == {}


def test_build_gitlab_instance_host_gives_intent(monkeypatch):
    """A repo on an institution's own GitLab instance gets that institution as
    `host` → intent True (and the nonprofit host halves the backing score); a
    gitlab.com repo gets nothing — commercial shared hosting backs nothing."""
    _base_mocks(monkeypatch, [
        E("debian/bzip2", repo_id="gl/salsa.debian.org-9696"),
        E("takluyver/jeepney", repo_id="gl/2780772"),
        E("rich/r"),
    ])
    monkeypatch.setattr(bf, "_load_gitlab_hosts",
                        lambda *a, **k: {"salsa.debian.org": ("debian.org", "nonprofit")})
    rows = {r["repo"]: r for r in bf.build()}
    salsa = rows["debian/bzip2"]
    assert (salsa["host"], salsa["host_type"]) == ("debian.org", "nonprofit")
    assert salsa["intent"] == "True"
    assert salsa["nonprofit"] == "True"
    assert salsa["host_score"] == "0.5"
    lab = rows["takluyver/jeepney"]
    assert (lab["host"], lab["host_type"]) == ("", "")
    assert lab["intent"] == "False"


def test_build_gitlab_funding_files_feed_yml_and_manifest(monkeypatch, tmp_path):
    """gitlab/funding-files.csv is the GitLab twin of funding-yml.csv: its
    FUNDING.yml flags flow into the same columns (→ intent), and an in-repo
    funding.json counts as the repo's own FLOSS manifest (→ has_funding_json,
    → the unmeasured-channel score cap)."""
    gl_file = tmp_path / "funding-files.csv"
    gl_file.write_text("x\n", encoding="utf-8")  # existence gate only; rows mocked
    def rows_by_repo(p):
        if "sponsors.csv" in str(p):
            return {"rich/r": {"gh_sponsorships_in": "100"}}
        if "funding-files.csv" in str(p):
            # keyed by the stable gl/ repo_id, like the real load_rows_by_id
            return {"gl/gitlab.gnome.org-658": {"has_funding_yml": "True",
                                                "has_funding_links": "True",
                                                "funding_link_platforms": "custom",
                                                "has_funding_json_file": "False"},
                    "gl/gitlab.inria.fr-22470": {"has_funding_yml": "False",
                                                 "has_funding_links": "False",
                                                 "funding_link_platforms": "",
                                                 "has_funding_json_file": "True"}}
        return {}
    _base_mocks(monkeypatch, [
        E("gnome/glib", repo_id="gl/gitlab.gnome.org-658"),
        E("mpc/mpc", repo_id="gl/gitlab.inria.fr-22470"),
        E("rich/r"),
    ])
    monkeypatch.setattr(bf, "load_rows_by_id", rows_by_repo)
    monkeypatch.setattr(bf, "GITLAB_FUNDING_FILE", gl_file)
    monkeypatch.setattr(bf, "_load_gitlab_hosts", lambda *a, **k: {})
    rows = {r["repo"]: r for r in bf.build()}
    glib = rows["gnome/glib"]
    assert glib["has_funding_yml"] == "True"
    assert glib["funding_link_platforms"] == "custom"
    assert glib["intent"] == "True"
    mpc = rows["mpc/mpc"]
    assert mpc["has_funding_json"] == "True"     # in-repo manifest file
    assert mpc["intent"] == "True"
    assert int(float(mpc["score"])) <= bf.DECLARED_FUNDING_CAP


def test_export_by_repo_maps_non_github_manifests_by_url(tmp_path):
    p = tmp_path / "funding-json.csv"
    p.write_text(
        "repo_id,project_repository,project_repository_resolved,channel_platforms\n"
        "gh/1,https://github.com/a/b,,github\n"
        ",https://GitLab.com/libtiff/libtiff.git/,,custom\n",
        encoding="utf-8")
    by_id, by_slug, by_url = bf._export_by_repo(p)
    assert "gh/1" in by_id
    assert by_url["gitlab.com/libtiff/libtiff"]["channel_platforms"] == "custom"
    assert not by_slug
