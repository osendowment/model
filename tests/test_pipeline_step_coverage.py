"""Guard: every source file a pipeline step READS is written by a pipeline step.

The bug this pins: eight producers were runnable only by hand
(`uv run python -m src.sources.…`), yet live steps read their output. Nothing
ever re-ran them, so their CSVs aged silently — repology's packages.csv was six
weeks stale, and fetch_canonical's scope query had drifted to zero repos without
anyone noticing, because no run ever touched it. A cache that no step refreshes
is not a cache; it is a fossil.

So: any producer wired in must (a) be a step of the stage that reads it, (b) run
before its consumer, and (c) carry a TTL so a warm re-run stays zero-network.
"""
import importlib

from src.eligibility.run_eligibility_pipeline import FETCHERS as ELIG_FETCHERS
from src.eligibility.run_eligibility_pipeline import BUILDERS as ELIG_BUILDERS
from src.risk.run_risk_pipeline import BUILDERS as RISK_BUILDERS
from src.value.run_value_pipeline import ROLLUP_LABELS, STEPS as VALUE_STEPS


def _order(steps) -> list[str]:
    return [s.label for s in steps]


def _by_module(steps) -> dict[str, object]:
    return {s.module: s for s in steps}


# ── value ────────────────────────────────────────────────────────────────────


def test_value_runs_the_sources_its_steps_read():
    """repology + ossfuzz feed the eco pipelines / git-urls; canonical feeds
    resolve; both criticality sources feed apply_criticality (80% of
    value_score). Each must be a net step, so --offline/--refresh reach it."""
    steps = _by_module(VALUE_STEPS)
    for module in ("src.sources.repology.fetch_repology_data",
                   "src.sources.ossfuzz.fetch_ossfuzz_data",
                   "src.sources.github.fetch_canonical",
                   "src.sources.openssf.criticality",
                   "src.sources.ecosystems.criticality"):
        assert module in steps, f"{module} is read by a value step but never run"
        assert steps[module].net, f"{module} must be net=True to honour --offline"


def test_value_step_order_puts_producers_before_consumers():
    order = _order(VALUE_STEPS)
    # repology/packages.csv is the cpp pipeline's distro-version input;
    # ossfuzz/projects.csv is a git-urls repo-URL source.
    assert order.index("repology") < order.index("cpp")
    assert order.index("ossfuzz") < order.index("git-urls")
    # canonical-repos.csv is applied by `resolve`.
    assert order.index("canonical") < order.index("resolve")
    # Criticality scope is class-A, which `unify` decides — and both files are
    # stamped onto value.csv by `criticality`.
    assert order.index("unify") < order.index("openssf-crit") < order.index("criticality")
    assert order.index("unify") < order.index("eco-crit") < order.index("criticality")


def test_rollup_covers_every_step_that_rebuilds_value_csv():
    """--rollup must not skip a value.csv input, or the fast path after an
    overrides.csv edit would silently rebuild with a stale identity/criticality."""
    for label in ("canonical", "openssf-crit", "eco-crit"):
        assert label in ROLLUP_LABELS


# ── risk ─────────────────────────────────────────────────────────────────────


def test_risk_extracts_the_scorecard_checks_it_scores():
    """openssf/checks.csv (Maintained / CI-Tests / Code-Review → workload) is a
    transform of the scorecard JSON. It must re-run with the builders, before
    build_workload reads it."""
    order = _order(RISK_BUILDERS)
    assert "openssf-checks" in order
    assert order.index("openssf-checks") < order.index("workload")


# ── eligibility ──────────────────────────────────────────────────────────────


def test_eligibility_fetches_the_maintainer_log_it_scores_on():
    """contributor-commits.csv drives `bf_maintainer_fundable` AND decides which
    logins fetch_maintainer_sponsors queries. Un-run, a repo entering scope has
    no contributor rows and silently scores as un-fundable."""
    order = _order(ELIG_FETCHERS)
    build = _order(ELIG_BUILDERS)
    assert "bf-contributors" in order
    assert order.index("bf-contributors") < order.index("maintainer-sponsors")
    assert "funding-build" in build            # runs after every fetcher
    assert "sponsorships" in order             # outbound sponsorships → build_funding


def test_eligibility_net_steps_honour_offline():
    steps = _by_module(ELIG_FETCHERS)
    for module in ("src.sources.github.fetch_contributors_metrics",
                   "src.sources.github.fetch_sponsorships"):
        assert steps[module].net, f"{module} must be net=True to honour --offline"


# ── TTLs ─────────────────────────────────────────────────────────────────────


def test_every_newly_wired_fetcher_carries_a_ttl():
    """A step with no TTL either refetches its whole scope every run (slow) or,
    worse, is skipped by hand forever (stale). Each of these gates on one."""
    expected = {
        # bf_fundable's maintainer log — 90 days, set by the model owner.
        "src.sources.github.fetch_contributors_metrics": 90,
        "src.sources.github.fetch_sponsorships": 90,
        "src.sources.github.fetch_canonical": 90,          # renames are rare
        "src.sources.ossfuzz.fetch_ossfuzz_data": 30,      # whole-file
        "src.sources.repology.fetch_repology_data": 30,    # whole-file
    }
    for module, ttl in expected.items():
        mod = importlib.import_module(module)
        assert getattr(mod, "TTL_DAYS", None) == ttl, f"{module}: TTL_DAYS != {ttl}"

    # The two criticality fetchers predate the sweep and keep their own names.
    from src.sources.ecosystems import criticality as eco_crit
    from src.sources.openssf import criticality as ossf_crit
    assert ossf_crit.TTL_DAYS >= 365      # criticality moves slowly
    assert eco_crit.DEFAULT_TTL_DAYS == 90
