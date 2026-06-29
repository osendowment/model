"""Tests for src/risk/build_funding.py — join, info cols, funding score."""

from dataclasses import dataclass

from src.risk import build_funding as bf


@dataclass
class E:
    repo: str
    repo_id: str = ""
    value_class: str = "B"


def test_oc_avg_funding_zero_default():
    assert bf.oc_avg_funding("", {}) == "0"                       # no slug → $0
    assert bf.oc_avg_funding("ghost", {"x": {"raised_2024": "5"}}) == "0"
    assert bf.oc_avg_funding("x", {"x": {"raised_2024": "10", "raised_2025": "20"}}) == "15"


def test_assemble_row_stars_forks_sponsorships():
    row = bf.assemble_row(
        repo="o/r", repo_id="1",
        sponsors={"github_sponsors": "12"},
        yml={"has_funding_link": "True", "funding_link_platforms": "github"},
        export={}, host="", host_type="", owner="", owner_type="",
        repo_meta={"stars": "5000", "forks": "300"},
        sponsoring_count="39",
    )
    assert row["has_funding_link"] == "True"
    assert row["org_fundable"] == "False"      # no org-level manifest
    assert row["gh_sponsors_in"] == "12"
    assert row["gh_sponsors_out"] == "39"
    assert row["gh_sponsorships"] == "51"      # in + out
    assert row["gh_stars"] == "5000"           # info column
    assert row["gh_forks"] == "300"            # info column
    assert row["oc_avg_funding"] == "0"        # no OC attributed → $0
    assert row["host_score"] == "1"            # no backing → ×1
    assert "score" not in row                  # filled by build()


def _base_mocks(monkeypatch, repos):
    """Shared setup: rich/r is funded (a repo-level OC worth $10k) so the two
    percentile axes vary. rich/r is in its OWN org so org-level attribution never
    credits the other (deliberately unfunded) test repos.
    """
    def rows_by_repo(p):
        if "sponsors.csv" in str(p):
            return {"rich/r": {"github_sponsors": "100"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bf, "load_rows_by_repo", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000"}})
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
            return {"rich/r": {"github_sponsors": "100"}}
        if "npm/funding.csv" in str(p):
            return {"npm/d": {"has_npm_funding": "True",
                              "npm_funding_url": "https://github.com/sponsors/x"}}
        if "pypi/funding.csv" in str(p):
            return {"pypi/d": {"has_pypi_funding": "True",
                               "pypi_funding_platforms": "github"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos",
                        lambda: [E("plain/p"), E("npm/d"), E("pypi/d"), E("rich/r")])
    monkeypatch.setattr(bf, "load_rows_by_repo", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100                  # unfunded, no channel
    for d in ("npm/d", "pypi/d"):
        assert rows[d]["channels_count"] == "1"             # registry channel counted
        assert rows[d]["score"] == bf.DECLARED_FUNDING_CAP  # 100 → 79


def test_build_funding_scraped_foundation_host_lowers_score(monkeypatch):
    """A scraped FOSS-foundation host (no override) defaults to nonprofit.

    plain/p and found/f are both unfunded on the two scored axes (0 sponsors,
    $0 OC) → both percentiles 100. found/f has a scraped foundation host, so its
    host_type defaults to nonprofit (host_score 0.5) and the backing enters the
    geom mean as a third axis at 50: round(∛(100·100·50)) = 79 (< the 100 plateau).
    """
    _base_mocks(monkeypatch, [E("plain/p"), E("found/f"), E("rich/r")])
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {"found/f": "apache"})

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
    _base_mocks(monkeypatch, [E("plain/p"), E("corp/c"), E("rich/r")])
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {
        "corp/c": {"host": "x.foundation", "host_type": "nonprofit",
                   "owner": "bigco.com", "owner_type": "company"}})

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["plain/p"]["score"] == 100
    assert rows["corp/c"]["owner_type"] == "company"
    assert rows["corp/c"]["host_score"] == "0"       # min(0.5, 0) = 0
    assert rows["corp/c"]["score"] == 1              # company backing → floor


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
    monkeypatch.setattr(bf, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bf, "load_rows_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {
        "aio-libs": {"raised_2024": "9000"}, "solo": {"raised_2024": "500"}})
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


def test_build_funding_override_oc_slug_authoritative(monkeypatch):
    """A curated override row is authoritative for OC, overriding the auto-map.

    - empty `oc_slug` on a curated repo → NO OC (suppresses a spurious match);
    - a non-empty `oc_slug` is used even if it differs from the reverse-map.
    """
    repos = [E("big/junkmatch", value_class="A"), E("big/keep", value_class="A")]
    monkeypatch.setattr(bf, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bf, "load_rows_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {
        "junk": {"raised_2024": "9999"}, "real": {"raised_2024": "200"}})
    # reverse-map would auto-match org `big` to the junk collective…
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({}, {"big": "junk"}))
    # …but overrides win: junkmatch → empty (no OC), keep → explicit slug.
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {
        "big/junkmatch": {"oc_slug": ""},
        "big/keep": {"oc_slug": "real"}})

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
            return {"link/lp": {"has_funding_link": "True", "funding_link_platforms": "liberapay"},
                    "gh/only": {"has_funding_link": "True", "funding_link_platforms": "github"}}
        if "sponsors.csv" in s:
            return {"rich/r": {"github_sponsors": "100"}}
        return {}
    monkeypatch.setattr(bf, "load_top_repos",
                        lambda: [E("link/lp"), E("gh/only"), E("plain/p"), E("rich/r")])
    monkeypatch.setattr(bf, "load_rows_by_repo", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_load_funding_overrides", lambda p: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_fundable_orgs", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc_index", lambda *a, **k: ({"rich/r": "rich"}, {}))

    rows = {r["repo"]: r for r in bf.build()}
    assert rows["link/lp"]["score"] == bf.DECLARED_FUNDING_CAP  # liberapay → capped 79
    assert rows["gh/only"]["score"] == 100   # github-only is measured → no cap, 0 sponsors
    assert rows["plain/p"]["score"] == 100   # no channel at all


def test_build_funding_org_fundable_caps_score_and_counts_channel(monkeypatch):
    """A fundable ORG (FLOSS manifest registered for the whole GitHub org) marks
    every repo it owns: org_fundable=True, its channels count, and the score is
    capped at DECLARED_FUNDING_CAP even with $0 measured. A repo in a different,
    non-fundable org stays at the 100 plateau.
    """
    _base_mocks(monkeypatch, [E("zulip/zulip"), E("zulip/zulip-mobile"),
                              E("plain/p"), E("rich/r")])
    # org `zulip` is fundable via an org-page manifest declaring liberapay+github.
    monkeypatch.setattr(bf, "_fundable_orgs",
                        lambda p: {"zulip": {"channel_platforms": "liberapay,github"}})

    rows = {r["repo"]: r for r in bf.build()}
    for repo in ("zulip/zulip", "zulip/zulip-mobile"):
        assert rows[repo]["org_fundable"] == "True"          # inherited by ALL org repos
        assert rows[repo]["channels_count"] == "2"           # org channels counted
        assert rows[repo]["score"] == bf.DECLARED_FUNDING_CAP  # 100 → 79 (cap)
    assert rows["plain/p"]["org_fundable"] == "False"
    assert rows["plain/p"]["score"] == 100                   # different org → no cap
