#!/usr/bin/env python3
"""ecosyste.ms criticality connector — per-repo criticality factor for top repos.

ecosyste.ms exposes criticality signals — a ``critical`` flag, a ``rankings``
percentile factor, and dependent counts — only on its PACKAGES API, keyed by
registry package; the repos API carries none of them. Rather than reverse-look
a repo's packages by URL (noisy: monorepos return 100+ junk claimants and the
endpoint 500s on them), this connector fetches the repo's *canonical* package
directly — the ``top_eco`` / ``top_eco_pkg`` the value stage already resolved —
so every row is the criticality of the one package that defines the repo.

One deliberate exception: the ``critical`` flag is curated per registry
package and many registries omit it (spack/conan/debian, plenty of
npm/pypi/crates packages), so a row can resolve with a rank but no flag.
For exactly those rows a repository_url lookup fallback scans the repo's
OTHER packages for an EXPLICIT flag (conda's ``openssl`` where spack has
none) — see ``pick_lookup_critical``. All numeric fields still come only
from the canonical package, and a flag is never derived from rank_average.

Writes one row per repo to:

    data/sources/ecosystems/criticality.csv

Scoped to the top repos (value class A by default), since criticality is an
importance signal for the head of the distribution. Works for GitHub and
GitLab-hosted repos alike — the GitLab C libraries resolve as ``cpp`` packages
(debian/spack/…), so they carry the same signal a GitHub npm package does.

Columns (one row per repo):
    repo_id, repository_url, class, valid, ecosystem, package, registry_hit,
    ok, error, critical, critical_via, rank_average, dependent_repos_count,
    dependent_packages_count, fetched_at

``critical_via`` is the registry that supplied the flag — equal to
``registry_hit`` when the canonical package carried it, another registry
(e.g. ``conda``) when the lookup fallback found it, blank when no explicit
flag exists anywhere.

``ok`` is the success flag. When False, ``error`` says why —
``not-found-in-any-registry`` (a genuine value-stage ↔ registry name mismatch,
a stable miss) vs ``fetch-error`` (a transient HTTP 5xx / timeout / bad JSON,
retried on the next run) — so a network blip is never silently recorded as a
real miss (auditability). ``critical`` is ecosyste.ms's critical-list flag
(blank when the registry omits it — unknown, not False); ``rank_average`` is a
percentile (lower = more important; e.g. express ≈ 0.08, an unranked distro
package ≈ 100). ``valid`` carries value.csv's validity so the class-A scope
(which deliberately includes not-yet-valid GitLab repos) stays filterable.

Usage:
    uv run python -m src.sources.ecosystems.criticality
    uv run python -m src.sources.ecosystems.criticality --classes A B --limit 20
    uv run python -m src.sources.ecosystems.criticality --ttl 0   # force refresh
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
OUT_FILE = DATA_DIR / "sources" / "ecosystems" / "criticality.csv"

ECOSYSTE_API = "https://packages.ecosyste.ms/api/v1"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"

# Value ecosystem → ordered ecosyste.ms registries to try (first 200 wins).
# npm/pypi/crates mirror src/sources/ecosystems/packages.py. cpp DIVERGES on
# purpose: packages.py tries debian first (widest coverage for URL backfill),
# but ecosyste.ms does not compute rankings for distro registries — every
# debian package reports rank_average=100. For the criticality signal we prefer
# spack/conan/vcpkg (which DO rank, e.g. bzip2=0.38 on spack vs 100 on debian),
# falling back to debian only for packages those registries don't carry.
REGISTRY_MAP: dict[str, list[str]] = {
    "npm":    ["npmjs.org"],
    "pypi":   ["pypi.org"],
    "crates": ["crates.io"],
    "cpp":    ["spack.io", "conan.io", "vcpkg.io", "debian-13", "debian-12"],
}

DEFAULT_TTL_DAYS = 90
DEFAULT_CONCURRENCY = 8

# value.csv's top_eco_pkg occasionally differs from the registry's canonical
# name (GNU/spack naming, or a library shipping under a lib* name), leaving the
# repo unresolved. These map a repository_url to the correct registry package
# name. Kept in code (not a data file) so the mapping travels with the fetcher;
# the ecosystem is unchanged — the mismatch is always the name. Add an entry
# when a class-A repo shows ok=False AND the package exists under another name:
# probe ALL cpp registries (spack/conan/vcpkg/debian) via the repository_url
# lookup (packages.ecosyste.ms/api/v1/packages/lookup?repository_url=) to find
# the real name before adding — don't conclude "un-indexed" from a partial probe.
PACKAGE_OVERRIDES: dict[str, str] = {
    "https://github.com/bdwgc/bdwgc": "bdw-gc",                          # boehm-gc → bdw-gc (spack)
    "https://github.com/freedesktop-unofficial-mirror/cairo": "cairo",  # cairo-graphics-library → cairo (spack)
    "https://gitlab.inria.fr/mpc/mpc": "mpc",                           # gnumpc → mpc (spack)
    "https://github.com/netflix/vmaf": "libvmaf",                       # vmaf → libvmaf (vcpkg)
}

CRIT_FIELDS = [
    "repo_id", "repository_url", "class", "valid", "ecosystem", "package",
    "registry_hit", "ok", "error", "critical", "critical_via", "rank_average",
    "dependent_repos_count", "dependent_packages_count", "fetched_at",
]


@dataclass
class Repo:
    repo_id: str
    repository_url: str
    cls: str
    valid: str
    ecosystem: str
    package: str


@dataclass
class CritResult:
    repo: Repo
    ok: bool = False
    registry_hit: str = ""
    critical: str = ""
    critical_via: str = ""   # registry that supplied the flag (blank if none)
    rank_average: str = ""
    dependent_repos_count: str = ""
    dependent_packages_count: str = ""
    fetched_at: str = ""
    error: str = ""


# ── repo selection ───────────────────────────────────────────────────────────


def _repo_url(git_url: str, repo_slug: str, platform: str) -> str:
    """Normalized repository URL — git_url wins, else GitHub slug; no ``.git``.

    ``repo_slug`` (value.csv's ``repo``) is only a GitHub fallback for the rare
    row whose ``git_url`` is blank; for every other platform a blank git_url
    yields a blank URL (the row is then skipped, having nothing to key on)."""
    u = git_url or (f"https://github.com/{repo_slug}"
                    if platform == "github" and repo_slug else "")
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/")


def load_repos(classes: set[str] | None, value_file: Path | None = None) -> list[Repo]:
    """Class-scoped repos from value.csv carrying their canonical package.

    Scopes on ``class`` alone — NOT via ``src.common.repos.load_top_repos`` — on
    purpose: that loader gates on ``git_valid`` and the GitHub/GitLab platform
    allow-list, but criticality is a package-importance factor, not a
    risk-completeness gate, so a not-yet-``valid``, other-host, or archived
    class-A repo still has a meaningful criticality. Each row carries value.csv's ``git_valid`` (as
    ``valid``) so a downstream consumer can re-apply the gate and the scope stays
    auditable.

    The unified ``repo_id`` (``gh/{id}`` / ``gl/{nickname}-{id}``) is read straight
    from value.csv — the value stage already resolved it for GitHub and GitLab
    rows alike, so no host-specific derivation is needed here.

    Only repos with a ``top_eco_pkg`` and a mapped ``top_eco`` registry are
    returned — a repo with no resolved package has nothing to score. Deduped by
    repository URL."""
    vf = value_file or VALUE_FILE
    if not vf.exists():
        log.warning("value.csv missing at %s", vf)
        return []
    out: list[Repo] = []
    seen: set[str] = set()
    with open(vf, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if classes and (r.get("class") or "").strip() not in classes:
                continue
            eco = (r.get("top_eco") or "").strip()
            pkg = (r.get("top_eco_pkg") or "").strip()
            if not pkg or eco not in REGISTRY_MAP:
                continue
            git_url = (r.get("git_url") or "").strip()
            platform = (r.get("platform") or "").strip().lower()
            slug = (r.get("repo") or "").strip()
            url = _repo_url(git_url, slug, platform)
            if not url or url.lower() in seen:
                continue
            seen.add(url.lower())
            out.append(Repo(
                repo_id=(r.get("repo_id") or "").strip(),
                repository_url=url, cls=(r.get("class") or "").strip(),
                valid=(r.get("git_valid") or "").strip(),
                ecosystem=eco, package=PACKAGE_OVERRIDES.get(url, pkg),
            ))
    return out


# ── cache I/O ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_fresh(fetched_at: str, ttl_days: int) -> bool:
    if not fetched_at or ttl_days <= 0:
        return False
    try:
        when = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return when >= dt.datetime.now(dt.UTC) - dt.timedelta(days=ttl_days)
    except ValueError:
        return False


def _cache_path(eco: str, pkg: str) -> Path:
    """One cache file per (ecosystem, package).

    Deliberately a SEPARATE cache from ``packages.py`` (which caches the same
    endpoint under ``sources/ecosystems/{eco}/raw/``). They cannot be shared:
    for cpp, packages.py queries debian first (widest URL coverage) while this
    fetcher queries spack/conan first — debian reports ``rank_average=100`` for
    every package, so reusing packages.py's cpp cache would silently zero out
    the criticality ranking for ~all cpp repos (verified: 57 cpp rows regress).
    """
    safe = pkg.replace("/", "__")
    return DATA_DIR / "sources" / "ecosystems" / "raw" / "criticality" / f"{eco}__{safe}.json"


def _read_cached(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cached(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── field extraction ─────────────────────────────────────────────────────────


def extract(payload: dict) -> dict[str, str]:
    """Pull the criticality fields out of a package's ecosyste.ms JSON.

    Every field keeps ``None`` (registry omitted it → unknown) distinct from a
    real value: blank means unknown, not zero/False."""
    avg = (payload.get("rankings") or {}).get("average")
    crit = payload.get("critical")
    dr = payload.get("dependent_repos_count")
    dp = payload.get("dependent_packages_count")
    return {
        "critical": "" if crit is None else str(bool(crit)),
        "rank_average": "" if avg is None else str(avg),
        "dependent_repos_count": "" if dr is None else str(dr),
        "dependent_packages_count": "" if dp is None else str(dp),
    }


# ── fetch ────────────────────────────────────────────────────────────────────


async def _fetch_one(session: aiohttp.ClientSession, repo: Repo,
                     sem: asyncio.Semaphore) -> CritResult:
    """Fetch the repo's canonical package (try each registry), cache, extract.

    Never raises — every failure is folded into an ``ok=False`` result so one
    bad response can't abort the batch. A ``fetch-error`` (transient HTTP
    5xx/429, timeout, or malformed JSON on any registry) is kept distinct from
    a clean ``not-found-in-any-registry`` (every registry returned 404): the
    former is retried on the next run and flagged in the CSV, so a network
    blip is never silently recorded as a genuine miss."""
    fetched_at = _now_iso()
    registries = REGISTRY_MAP.get(repo.ecosystem, [])
    payload: dict | None = None
    registry_hit = ""
    transient = False
    async with sem:
        for reg in registries:
            q_reg = urllib.parse.quote(reg, safe="")
            q_pkg = urllib.parse.quote(repo.package, safe="")
            url = f"{ECOSYSTE_API}/registries/{q_reg}/packages/{q_pkg}"
            for attempt in range(2):  # one retry per registry on a transient error
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        if resp.status == 200:
                            try:
                                body = await resp.json()
                            except (aiohttp.ClientError, ValueError) as e:
                                transient = True
                                log.debug("%s @ %s: bad JSON: %s", repo.package, reg, e)
                                break
                            if isinstance(body, dict) and body.get("name"):
                                payload, registry_hit = body, reg
                            break  # 200 (with or without a name) — no retry
                        if resp.status == 404:
                            break  # clean miss for this registry; try the next
                        if resp.status in (429, 500, 502, 503, 504):
                            transient = True
                            if attempt == 0:
                                await asyncio.sleep(2)
                                continue  # retry the same registry once
                            break
                        log.debug("%s @ %s: HTTP %s", repo.package, reg, resp.status)
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    transient = True
                    log.debug("%s @ %s: %s", repo.package, reg, e)
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    break
            if payload is not None:
                break

    if payload is None:
        return CritResult(
            repo=repo, ok=False, fetched_at=fetched_at,
            error="fetch-error" if transient else "not-found-in-any-registry",
        )

    try:
        _write_cached(_cache_path(repo.ecosystem, repo.package), {
            "ecosystem": repo.ecosystem, "package": repo.package,
            "registry_hit": registry_hit, "fetched_at": fetched_at, "data": payload,
        })
    except OSError as e:  # a cache write failure must not lose the fetched result
        log.warning("cache write failed for %s/%s: %s", repo.ecosystem, repo.package, e)
    fields = extract(payload)
    return CritResult(repo=repo, ok=True, registry_hit=registry_hit,
                      critical_via=registry_hit if fields["critical"] else "",
                      fetched_at=fetched_at, **fields)


def _result_from_cache(repo: Repo, cached: dict) -> CritResult:
    data = cached.get("data") or {}
    fields = extract(data)
    return CritResult(repo=repo, ok=True, registry_hit=cached.get("registry_hit", ""),
                      critical_via=(cached.get("registry_hit", "")
                                    if fields["critical"] else ""),
                      fetched_at=cached.get("fetched_at", ""), **fields)


# ── critical-flag fallback (repository_url lookup) ──────────────────────────
#
# The `critical` flag is ecosyste.ms' curated list membership, set per
# registry package — many registries omit it (spack/conan/debian, and plenty
# of npm/pypi/crates packages), so a repo's canonical package can resolve
# with a rank but no flag. Some OTHER package of the same repo often carries
# it (conda's `openssl` where spack omits it; npm's `vitest` where
# `@vitest/pretty-format` has none). The fallback asks the repository_url
# lookup for every package of the repo and takes ONLY an explicit flag —
# rank_average is never converted into one.


def _lookup_cache_path(repo: Repo) -> Path:
    safe = repo.repository_url.split("://", 1)[-1].replace("/", "__")
    return (DATA_DIR / "sources" / "ecosystems" / "raw" / "criticality"
            / "lookup" / f"{safe}.json")


def _lookup_cache_fresh(repo: Repo, ttl_days: int) -> bool:
    cached = _read_cached(_lookup_cache_path(repo))
    return bool(cached) and _is_fresh(cached.get("fetched_at", ""), ttl_days)


def pick_lookup_critical(packages: list[dict]) -> tuple[str, str]:
    """Explicit critical flag across a repo's lookup packages → (flag, via).

    Any package with ``critical=True`` wins — the repo ships at least one
    critical package. Otherwise an explicit ``critical=False`` counts as a
    checked "not on the list". Otherwise blank: unknown stays unknown."""
    false_via = ""
    for p in packages:
        via = str(p.get("registry_name") or p.get("ecosystem") or "")
        if p.get("critical") is True:
            return "True", via
        if p.get("critical") is False and not false_via:
            false_via = via
    return ("False", false_via) if false_via else ("", "")


async def _lookup_packages(session: aiohttp.ClientSession | None, repo: Repo,
                           sem: asyncio.Semaphore,
                           ttl_days: int) -> list[dict] | None:
    """The repo's packages from the lookup endpoint, trimmed and cached.

    Returns None on a transient failure (no cache write — retried next run);
    a clean empty list is cached like any other answer. With ``session=None``
    only the cache is consulted (the no-network path)."""
    cached = _read_cached(_lookup_cache_path(repo))
    if cached and _is_fresh(cached.get("fetched_at", ""), ttl_days):
        return cached.get("packages", [])
    if session is None:
        return None
    q_url = urllib.parse.quote(repo.repository_url, safe="")
    url = f"{ECOSYSTE_API}/packages/lookup?repository_url={q_url}"
    async with sem:
        for attempt in range(2):
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        if resp.status in (429, 500, 502, 503, 504) and attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        log.debug("lookup %s: HTTP %s", repo.repository_url, resp.status)
                        return None
                    body = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                log.debug("lookup %s: %s", repo.repository_url, e)
                return None
            packages = [
                {"registry_name": p.get("registry_name") or p.get("ecosystem") or "",
                 "name": p.get("name") or "", "critical": p.get("critical")}
                for p in body if isinstance(p, dict)
            ] if isinstance(body, list) else []
            try:
                _write_cached(_lookup_cache_path(repo), {
                    "repository_url": repo.repository_url,
                    "fetched_at": _now_iso(), "packages": packages,
                })
            except OSError as e:
                log.warning("lookup cache write failed for %s: %s",
                            repo.repository_url, e)
            return packages
    return None


# ── output ───────────────────────────────────────────────────────────────────


def _read_out() -> dict[str, dict[str, str]]:
    if not OUT_FILE.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(OUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["repository_url"]] = row
    return out


def _row(r: CritResult) -> dict[str, str]:
    return {
        "repo_id": r.repo.repo_id, "repository_url": r.repo.repository_url,
        "class": r.repo.cls, "valid": r.repo.valid, "ecosystem": r.repo.ecosystem,
        "package": r.repo.package, "registry_hit": r.registry_hit, "ok": str(r.ok),
        "error": r.error, "critical": r.critical, "critical_via": r.critical_via,
        "rank_average": r.rank_average,
        "dependent_repos_count": r.dependent_repos_count,
        "dependent_packages_count": r.dependent_packages_count, "fetched_at": r.fetched_at,
    }


def _write_out(rows: dict[str, dict[str, str]]) -> None:
    """Atomic write (tmp + os.replace) so a crash mid-write can't corrupt the
    file and lose prior rows for repos untouched this run."""
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        # restval: rows read from an older file may predate `critical_via`.
        w = csv.DictWriter(f, fieldnames=CRIT_FIELDS, extrasaction="ignore",
                           restval="", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for url in sorted(rows):
            w.writerow(rows[url])
    os.replace(tmp, OUT_FILE)


# ── orchestration ────────────────────────────────────────────────────────────


@dataclass
class RunStats:
    cached: int = 0
    fetched: int = 0
    fetch_errors: int = 0   # transient (retryable) failures this run
    not_found: int = 0      # package matched no registry (name mismatch)
    critical: int = 0
    found: int = 0
    backfilled: int = 0     # blank flags filled by the repository_url lookup
    results: list[CritResult] = field(default_factory=list)


async def run(classes: set[str] | None, ttl_days: int, concurrency: int,
              limit: int | None = None, offline: bool = False) -> RunStats:
    repos = load_repos(classes)
    if limit:
        repos = repos[:limit]
    stats = RunStats()
    if not repos:
        console.print("[yellow]No repos matched (check --classes / value.csv).[/yellow]")
        return stats

    to_fetch: list[Repo] = []
    cached_results: dict[str, CritResult] = {}
    for repo in repos:
        cache = _read_cached(_cache_path(repo.ecosystem, repo.package))
        # Offline: a stale cache still beats no answer — the run must never
        # touch the network, so an expired row is served rather than refetched.
        if cache and (offline or _is_fresh(cache.get("fetched_at", ""), ttl_days)):
            cached_results[repo.repository_url] = _result_from_cache(repo, cache)
        elif not offline:
            to_fetch.append(repo)

    scope = "/".join(sorted(classes)) if classes else "all"
    console.print(f"[bold]criticality[/bold]  [dim]classes={scope} | {len(repos):,} repos | "
                  f"{len(cached_results):,} cached | {len(to_fetch):,} to fetch"
                  + (" | offline" if offline else "") + "[/dim]")

    fetched: dict[str, CritResult] = {}
    sem = asyncio.Semaphore(concurrency)

    def _progress() -> Progress:
        return Progress(
            SpinnerColumn(), BarColumn(bar_width=16), TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"), TimeElapsedColumn(),
            TextColumn("[dim]{task.description}[/]"), console=console,
        )

    async def _fetch_all(session: aiohttp.ClientSession) -> None:
        progress = _progress()
        with progress:
            task = progress.add_task("package", total=len(to_fetch))

            async def _go(repo: Repo) -> None:
                res = await _fetch_one(session, repo, sem)
                fetched[repo.repository_url] = res
                progress.update(task, advance=1, description=repo.package[:26])

            await asyncio.gather(*[_go(r) for r in to_fetch])

    # Fallback: a resolved row without a critical flag gets a repository_url
    # lookup — another package of the same repo may carry the explicit flag.
    # Only explicit flags are taken; rank_average is never converted into one
    # (unknown stays unknown).
    async def _backfill_flags(session: aiohttp.ClientSession | None,
                              need_flag: list[CritResult]) -> None:
        progress = _progress()
        with progress:
            task = progress.add_task("flag lookup", total=len(need_flag))

            async def _fill(res: CritResult) -> None:
                packages = await _lookup_packages(session, res.repo, sem, ttl_days)
                if packages is not None:
                    flag, via = pick_lookup_critical(packages)
                    if flag:
                        res.critical, res.critical_via = flag, via
                        stats.backfilled += 1
                progress.update(task, advance=1, description=res.repo.package[:26])

            await asyncio.gather(*[_fill(r) for r in need_flag])

    def _flagless(results: dict[str, CritResult]) -> list[CritResult]:
        return [r for r in results.values() if r.ok and not r.critical]

    # A session is opened only when something actually needs HTTP — primary
    # fetches, or a flagless row whose lookup cache is stale. A fully-fresh
    # run (packages AND lookups cached) stays off the network entirely.
    lookup_needs_http = not offline and any(
        not _lookup_cache_fresh(r.repo, ttl_days)
        for r in _flagless(cached_results))
    if to_fetch or lookup_needs_http:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as session:
            if to_fetch:
                await _fetch_all(session)
            all_results = {**cached_results, **fetched}
            need_flag = _flagless(all_results)
            if need_flag:
                await _backfill_flags(session, need_flag)
    else:
        all_results = dict(cached_results)
        need_flag = _flagless(all_results)
        if need_flag:  # every lookup cache is fresh — apply without HTTP
            await _backfill_flags(None, need_flag)

    out = _read_out()
    for url, res in all_results.items():
        out[url] = _row(res)
    _write_out(out)

    stats.cached = len(cached_results)
    stats.fetched = len(fetched)
    stats.results = list(all_results.values())
    stats.fetch_errors = sum(1 for r in stats.results if r.error == "fetch-error")
    stats.not_found = sum(1 for r in stats.results
                          if r.error == "not-found-in-any-registry")
    stats.critical = sum(1 for r in stats.results if r.critical == "True")
    stats.found = sum(1 for r in stats.results if r.ok)
    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────


def _to_int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _summary_table(stats: RunStats) -> Table:
    t = Table(title="[bold]ecosyste.ms criticality[/bold]", show_header=True,
              header_style="bold dim", padding=(0, 1))
    t.add_column("package", style="bold", overflow="fold")
    t.add_column("eco", justify="center")
    t.add_column("critical", justify="center")
    t.add_column("rank_avg", justify="right")
    t.add_column("dep_repos", justify="right")
    top = sorted(
        [r for r in stats.results if r.ok],
        key=lambda r: _to_int(r.dependent_repos_count), reverse=True,
    )[:20]
    for r in top:
        crit = "[green]yes[/]" if r.critical == "True" else "[dim]—[/]"
        t.add_row(r.repo.package, r.repo.ecosystem, crit,
                  r.rank_average[:6], f"{_to_int(r.dependent_repos_count):,}")
    return t


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--classes", nargs="+", default=["A"],
                        help="value classes to include (default: A; 'all' for every class)")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"skip refetch if cached within N days (default: {DEFAULT_TTL_DAYS}, 0 to force)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"concurrent in-flight requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--limit", type=int, help="process only first N repos (testing)")
    # The pipeline runs this as a net=True step, which appends --offline/--refresh.
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the TTL and refetch everything (same as --ttl 0)")
    parser.add_argument("--offline", action="store_true",
                        help="never fetch — serve whatever is cached, stale included")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    classes = None if args.classes == ["all"] else set(args.classes)
    ttl = 0 if args.refresh else args.ttl
    t_start = time.monotonic()
    stats = asyncio.run(run(classes, ttl_days=ttl, concurrency=args.concurrency,
                            limit=args.limit, offline=args.offline))

    console.print()
    console.print(_summary_table(stats))
    console.print(
        f"[dim]cached={stats.cached:,} fetched={stats.fetched:,} | "
        f"found={stats.found:,} critical={stats.critical:,} "
        f"flag-backfilled={stats.backfilled:,} "
        f"not-found={stats.not_found:,} fetch-errors={stats.fetch_errors:,} | "
        f"{time.monotonic() - t_start:.1f}s[/dim]"
    )
    console.print(f"[green]→ {OUT_FILE}[/green]")


if __name__ == "__main__":
    main()
