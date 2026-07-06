"""
Fetch npm download + dependency data, iterating until the graph is complete.

Each round:
  1. Find packages that appear as deps but have no download data → fetch downloads
  2. Find packages with downloads whose deps haven't been fetched → fetch deps
  Repeats until no gaps remain.

State is persisted every 20 packages — safe to interrupt and resume.

Run:
    uv run src/sources/npm/fetch_npm_data.py
    uv run src/sources/npm/fetch_npm_data.py --max-rounds 3
    uv run src/sources/npm/fetch_npm_data.py --concurrency 20
    uv run src/sources/npm/fetch_npm_data.py --limit 50   # test mode

NPM_TOKEN (optional, in .env) Bearer-auths requests to registry.npmjs.org
(dependency lookups) for a higher rate limit there. api.npmjs.org/downloads
is a separate, public unauthenticated stats endpoint and ignores it.
"""

import argparse
import asyncio
import csv
import os
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

load_dotenv()

RAW_DOWNLOADS    = "data/sources/npm/raw/downloads.csv"
DOWNLOADS_STATUS = "data/sources/npm/raw/downloads.status.csv"
RAW_DEPS         = "data/sources/npm/raw/dependencies.csv"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point"
NPM_REGISTRY  = "https://registry.npmjs.org"
YEARS         = [2021, 2022, 2023, 2024, 2025]
BATCH_SIZE    = 128
# npm publishes no fixed req/s number for either host (their docs only commit
# to 128 pkgs/365 days per bulk downloads call, already BATCH_SIZE). Empirically
# verified: a sustained 1.0 req/s held clean (60/60 requests, 0 429s over 94s),
# while 2.0 req/s sustained triggered continuous 429s (confirmed via isolated
# reproduction — not a burst/concurrency effect, and not a long-lived IP block:
# short bursts at 2.0 succeeded too; only sustained load surfaced it). Rate
# limiter enforces this globally regardless of CONCURRENCY, so concurrency only
# affects in-flight parallelism, not throughput.
CONCURRENCY   = 3
# Observed npm behaviour: a freshly started client gets ~90-120s of solid 429s
# before requests start flowing (rolling-window budget), so retries must
# survive that warm-up. 5 tiers ≈ 200s of cumulative tolerance.
MAX_RETRIES   = 5
RETRY_BACKOFF = [5, 15, 30, 60, 90]
RATE_PER_SEC  = 1.0
DEPS_TTL_DAYS = 365  # re-fetch a package's dep edges after this age — /latest deps drift with releases
DOWNLOADS_STATUS_TTL_DAYS = 365  # sidecar verdicts (ok / not_found) older than this are re-checkable
USER_AGENT    = "osendowment-model/1.0 (research; +https://endowment.dev)"
NPM_TOKEN     = os.environ.get("NPM_TOKEN", "")

FETCH_FAILED = object()  # sentinel: retries exhausted, no real answer — must never stand in for a real 0/empty

console = Console()


# ── I/O helpers ───────────────────────────────────────────────────────────────

def load_raw_downloads() -> dict[tuple[str, int], dict]:
    """Return {(package, year): row} from raw/downloads.csv."""
    if not os.path.exists(RAW_DOWNLOADS):
        return {}
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        return {
            (row["package"], int(row["year"])): {
                "package": row["package"], "year": row["year"], "downloads": row["downloads"],
            }
            for row in csv.DictReader(f)
        }


def packages_with_all_years(raw_dl: dict) -> set[str]:
    """Packages that have data for all 5 years."""
    pkg_years: dict[str, set] = defaultdict(set)
    for (pkg, year) in raw_dl:
        pkg_years[pkg].add(year)
    return {pkg for pkg, years in pkg_years.items() if set(YEARS) <= years}


def write_raw_downloads(raw_dl: dict[tuple[str, int], dict]) -> None:
    tmp = RAW_DOWNLOADS + ".tmp"
    os.makedirs(os.path.dirname(RAW_DOWNLOADS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["package", "year", "downloads"],
                           extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(raw_dl.values(), key=lambda r: (r["package"], int(r["year"]))))
    os.replace(tmp, RAW_DOWNLOADS)


def load_downloads_status() -> dict[str, dict]:
    """Return {package: {"status": ok|not_found, "checked_at": iso}} from the sidecar.

    downloads.csv rows are bare (package, year, downloads) — a 0 there cannot
    distinguish "measured zero" from "package 404s on npm". The sidecar records
    that per-package verdict + fetch date, so zero-audits can skip packages
    already checked within DOWNLOADS_STATUS_TTL_DAYS instead of re-fetching
    every all-zero package forever.
    """
    if not os.path.exists(DOWNLOADS_STATUS):
        return {}
    with open(DOWNLOADS_STATUS, newline="", encoding="utf-8") as f:
        return {row["package"]: {"status": row["status"], "checked_at": row["checked_at"]}
                for row in csv.DictReader(f) if row.get("package")}


def write_downloads_status(status: dict[str, dict]) -> None:
    tmp = DOWNLOADS_STATUS + ".tmp"
    os.makedirs(os.path.dirname(DOWNLOADS_STATUS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["package", "status", "checked_at"])
        for pkg in sorted(status):
            w.writerow([pkg, status[pkg]["status"], status[pkg]["checked_at"]])
    os.replace(tmp, DOWNLOADS_STATUS)


def status_fresh_packages(status: dict[str, dict],
                          ttl_days: int = DOWNLOADS_STATUS_TTL_DAYS) -> set[str]:
    """Packages whose sidecar verdict is within `ttl_days` — safe to skip in audits."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ttl_days)
    fresh: set[str] = set()
    for pkg, row in status.items():
        try:
            if datetime.fromisoformat(row["checked_at"]) >= cutoff:
                fresh.add(pkg)
        except (ValueError, KeyError):
            continue
    return fresh


def load_fetched_dep_packages(ttl_days: int = DEPS_TTL_DAYS) -> set[str]:
    """Packages whose deps were fetched within `ttl_days`.

    Dep edges come from `/{package}/latest`, so they drift as packages
    release new versions — an edge list older than the TTL no longer
    reflects the package's current dependencies. Stale (or unstamped)
    rows are treated as unfetched, so the next round re-fetches them and
    the graph frontier tracks upstream reality. `ttl_days <= 0` disables
    the TTL (every recorded package counts as fetched).
    """
    if not os.path.exists(RAW_DEPS):
        return set()
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=ttl_days)) if ttl_days > 0 else None
    fresh: set[str] = set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pkg = row.get("package")
            if not pkg or pkg in fresh:
                continue
            if cutoff is None:
                fresh.add(pkg)
                continue
            try:
                # stored as naive-UTC "%Y-%m-%d %H:%M:%S.%f"
                fetched = datetime.fromisoformat(row.get("fetched_at", ""))
            except ValueError:
                continue  # unstamped/unparsable — treat as stale
            if fetched >= cutoff:
                fresh.add(pkg)
    return fresh


def load_all_dep_names() -> set[str]:
    """All dep_name values seen in raw/deps (excludes __none__ placeholders)."""
    deps = load_raw_deps()
    return {dep_name for deps_list in deps.values() for dep_name, _, _ in deps_list
            if dep_name and dep_name != "__none__"}


def load_raw_deps() -> dict[str, list[tuple[str, str, str]]]:
    """Return {package: [(dep_name, dep_version, fetched_at)]} from raw/deps."""
    if not os.path.exists(RAW_DEPS):
        return {}
    data: dict[str, list] = defaultdict(list)
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data[row["package"]].append((row["dep_name"], row["dep_version"], row.get("fetched_at", "")))
    return data


def write_raw_deps(deps: dict[str, list[tuple[str, str, str]]]) -> None:
    """Write {package: [(dep_name, dep_version, fetched_at)]} atomically."""
    tmp = RAW_DEPS + ".tmp"
    os.makedirs(os.path.dirname(RAW_DEPS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["package", "dep_name", "dep_version", "fetched_at"])
        for pkg in sorted(deps):
            for dep_name, dep_ver, fetched_at in deps[pkg]:
                w.writerow([pkg, dep_name, dep_ver, fetched_at])
    os.replace(tmp, RAW_DEPS)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate: float):
        self._rate = rate
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._pause_until = 0.0

    def pause(self, seconds: float) -> None:
        """Hold ALL requests for `seconds` — call on a 429 so concurrent tasks
        wait out the throttle window together instead of each burning a
        request (and collecting its own 429) probing the same window. npm's
        Retry-After is always 0, so callers pass their backoff tier."""
        now = asyncio.get_event_loop().time()
        self._pause_until = max(self._pause_until, now + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now  = asyncio.get_event_loop().time()
                wait = max(self._last + 1.0 / self._rate - now,
                           self._pause_until - now)
                if wait <= 0:
                    break
                await asyncio.sleep(wait)  # loop: pause_until may extend while sleeping
            self._last = asyncio.get_event_loop().time()


def _backoff_wait(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to sleep before retrying. Honours a server Retry-After header
    when present and positive (some 429s send "0", which is not a real
    signal to retry immediately — fall back to our schedule instead), else
    our fixed schedule; jitter desyncs concurrent requests that would
    otherwise all retry in lockstep."""
    parsed = float(retry_after) if retry_after and retry_after.isdigit() else None
    base = parsed if parsed and parsed > 0 else RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
    return base + random.uniform(0, base * 0.2)


class FetchProgress:
    """Shared counters for periodic plain-text status logging.

    tqdm's carriage-return progress bar is unreadable once stdout is
    redirected to a log file (every update overwrites the same line, so a
    file tail shows one giant line). This gives a plain, newline-terminated
    summary a log-file reader can grep/tail directly, without needing to know
    it must convert \\r to \\n first.
    """

    def __init__(self) -> None:
        self.waits = 0
        self.exhausted = 0
        self.ok = 0          # HTTP 200 with data
        self.not_found = 0   # HTTP 404 (package/year didn't exist)
        self.timeouts = 0    # request timed out / transport error
        self.errors = 0      # other HTTP errors

    def wait(self) -> None:
        self.waits += 1

    def exhaust(self) -> None:
        self.exhausted += 1


async def _log_status_periodically(
    label: str, bar: tqdm, progress: FetchProgress, interval: float = 20.0,
    raw_dl: dict | None = None, flush_every: int = 3,
) -> None:
    """Print one clean status line every `interval` seconds until cancelled.

    When `raw_dl` is given, also flush it to disk every `flush_every` ticks so
    a killed/crashed run keeps its partial per-year data instead of losing
    everything fetched since the last completion-gated save.
    """
    t0 = time.monotonic()
    tick = 0
    while True:
        await asyncio.sleep(interval)
        tick += 1
        print(
            f"[status] {label} t={time.monotonic() - t0:.0f}s "
            f"done={bar.n}/{bar.total} ok={progress.ok} 404={progress.not_found} "
            f"429_waits={progress.waits} timeouts={progress.timeouts} "
            f"errors={progress.errors} exhausted={progress.exhausted}",
            flush=True,
        )
        if raw_dl is not None and tick % flush_every == 0:
            write_raw_downloads(raw_dl)


# ── npm downloads API ─────────────────────────────────────────────────────────

def batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def fetch_downloads_batch(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    year: int,
    pkgs: list[str],
    bar: tqdm | None = None,
    progress: FetchProgress | None = None,
) -> tuple[int, dict[str, int | None]]:
    """Return {pkg: count} where None means the package didn't exist that year,
    FETCH_FAILED means retries were exhausted — never a real 0."""
    url = f"{NPM_DOWNLOADS}/{year}-01-01:{year}-12-31/{','.join(pkgs)}"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 404:
                        if progress: progress.not_found += 1
                        return year, {p: None for p in pkgs}
                    if r.status == 429:
                        if progress: progress.wait()
                        wait = _backoff_wait(attempt, r.headers.get("Retry-After"))
                        rl.pause(wait)  # hold ALL tasks — don't let others probe the same window
                        if bar: bar.set_description(f"  downloads [rate limit — waiting {wait:.0f}s]")
                        await asyncio.sleep(wait)
                        if bar: bar.set_description("  downloads")
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    if progress: progress.ok += 1
                    if "downloads" in data:
                        # single-package response — None means not found
                        dl = data.get("downloads")
                        return year, {pkgs[0]: None if dl is None else dl}
                    # bulk response — info=None means package didn't exist
                    return year, {
                        p: None if info is None else (info.get("downloads") or 0)
                        for p, info in data.items()
                    }
            except asyncio.TimeoutError:
                if progress: progress.timeouts += 1
                if attempt < MAX_RETRIES - 1:
                    wait = _backoff_wait(attempt)
                    if bar: bar.set_description(f"  downloads [timeout — retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s]")
                    await asyncio.sleep(wait)
                    if bar: bar.set_description("  downloads")
            except Exception:
                if progress: progress.errors += 1
                if attempt < MAX_RETRIES - 1:
                    wait = _backoff_wait(attempt)
                    if bar: bar.set_description(f"  downloads [retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s]")
                    await asyncio.sleep(wait)
                    if bar: bar.set_description("  downloads")
    if progress: progress.exhaust()
    return year, {p: FETCH_FAILED for p in pkgs}  # retries exhausted — unresolved, retried next round


SAVE_INTERVAL = 50  # save raw_dl to disk every N completed packages


def _apply_short_circuit(
    pkg: str,
    yr_data: dict[int, int | None],
    raw_dl: dict,
) -> None:
    """Write all years for one package, filling zeros for years before first None."""
    for year in sorted(YEARS, reverse=True):
        count = yr_data.get(year)
        raw_dl[(pkg, year)] = {
            "package": pkg, "year": year,
            "downloads": 0 if count is None else count,
        }
        if count is None:
            for earlier in YEARS:
                if earlier < year:
                    raw_dl[(pkg, earlier)] = {"package": pkg, "year": earlier, "downloads": 0}
            return


async def fetch_package_downloads(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    pkg: str,
    raw_dl: dict,
    bar: tqdm | None = None,
    progress: FetchProgress | None = None,
    status: dict[str, dict] | None = None,
) -> bool:
    """Fetch one scoped package's missing years sequentially, newest first.

    npm's bulk endpoint rejects scoped names ("scoped packages are not
    currently supported in bulk lookups"), so one request per (package, year)
    is the minimum. Fetching a package's years inside ONE task — rather than
    scattering them across a year-grouped task list — means packages complete
    (and get persisted) steadily from the start of the run instead of only
    after the final year's task queue drains at the very end.

    Writes fetched years straight into raw_dl, never touching years already
    present. A 404 year means the package didn't exist then — that year and
    all earlier missing years are recorded as 0 without further requests.
    Returns True if the package now has all YEARS present, False if a fetch
    exhausted retries (gap left for the next round — never a fake 0).
    """
    # `sem` gates WHOLE packages, not individual requests. asyncio semaphores
    # are FIFO: were each year-request to re-acquire a shared request-level
    # semaphore, a package's year-2 request would queue behind every other
    # package's year-1 request, degrading execution to year-grouped order —
    # where no package completes until the very end of the run. Holding the
    # slot for all of a package's years keeps completions steady.
    async with sem:
        inner = asyncio.Semaphore(1)  # fetch_downloads_batch requires a sem; years here run sequentially anyway
        needed = [y for y in sorted(YEARS, reverse=True) if (pkg, y) not in raw_dl]
        gone = False
        for i, year in enumerate(needed):
            _, res = await fetch_downloads_batch(session, inner, rl, year, [pkg], bar, progress)
            count = res[pkg]
            if count is FETCH_FAILED:
                return False
            if count is None:
                # didn't exist this year — this and all earlier missing years are 0.
                # None at the NEWEST model year means the package 404s outright.
                gone = year == max(YEARS)
                for y2 in needed[i:]:
                    raw_dl[(pkg, y2)] = {"package": pkg, "year": y2, "downloads": 0}
                break
            raw_dl[(pkg, year)] = {"package": pkg, "year": year, "downloads": count}
        if status is not None:
            status[pkg] = {"status": "not_found" if gone else "ok",
                           "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")}
        return True


async def _fetch_downloads_async(
    packages: list[str],
    concurrency: int,
    raw_dl: dict,
    bar: tqdm,
) -> int:
    """One session; bulk batches for unscoped, one sequential task per scoped
    package. Saves raw_dl every SAVE_INTERVAL completed packages. Returns the
    number of packages left unresolved (retries exhausted)."""
    unscoped = [p for p in packages if not p.startswith("@")]
    scoped   = [p for p in packages if p.startswith("@")]

    sem        = asyncio.Semaphore(concurrency)
    rl         = RateLimiter(RATE_PER_SEC)
    progress   = FetchProgress()
    status     = load_downloads_status()
    n_done     = 0
    unresolved = 0

    def _stamp(pkg: str, newest_none: bool) -> None:
        status[pkg] = {"status": "not_found" if newest_none else "ok",
                       "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")}

    def _flush() -> None:
        write_raw_downloads(raw_dl)
        write_downloads_status(status)

    # DummyCookieJar: do NOT persist Cloudflare's per-session _cfuvid cookie.
    # A long-running session that carries a rate-limit-flagged _cfuvid keeps
    # being throttled on every retry, while a cookieless client (like curl)
    # gets a fresh budget — observed directly: curl succeeded instantly while
    # a cookied session was stuck in continuous 429s from the same IP.
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        status_task = asyncio.ensure_future(
            _log_status_periodically("downloads", bar, progress, raw_dl=raw_dl))
        try:
            # ── unscoped: bulk-batched, ~5 requests per 128 packages ──
            if unscoped:
                pkg_yrs: dict[str, dict[int, int | None]] = {p: {} for p in unscoped}
                waiting: dict[str, int] = {p: len(YEARS) for p in unscoped}
                futs = [
                    asyncio.ensure_future(fetch_downloads_batch(session, sem, rl, y, b, bar, progress))
                    for y in YEARS for b in batches(unscoped, BATCH_SIZE)
                ]
                for fut in asyncio.as_completed(futs):
                    year, res = await fut
                    newly_complete: list[str] = []
                    for pkg, count in res.items():
                        if year in pkg_yrs[pkg]:
                            continue  # already filled by short-circuit
                        if count is FETCH_FAILED:
                            continue  # unresolved — retried next round
                        pkg_yrs[pkg][year] = count
                        waiting[pkg] -= 1
                        if count is None:
                            # package didn't exist this year — pre-fill earlier years
                            for earlier in YEARS:
                                if earlier < year and earlier not in pkg_yrs[pkg]:
                                    pkg_yrs[pkg][earlier] = 0
                                    waiting[pkg] -= 1
                        if waiting[pkg] == 0:
                            newly_complete.append(pkg)
                    for pkg in newly_complete:
                        _apply_short_circuit(pkg, pkg_yrs[pkg], raw_dl)
                        _stamp(pkg, pkg_yrs[pkg].get(max(YEARS)) is None)
                        bar.update(1)
                        n_done += 1
                    if newly_complete and n_done % SAVE_INTERVAL < len(newly_complete):
                        _flush()
                unresolved += sum(1 for p in unscoped if waiting[p] > 0)

            # ── scoped: one task per package, its years fetched sequentially ──
            if scoped:
                futs = [
                    asyncio.ensure_future(
                        fetch_package_downloads(session, sem, rl, p, raw_dl, bar, progress, status))
                    for p in scoped
                ]
                for fut in asyncio.as_completed(futs):
                    if not await fut:
                        unresolved += 1
                        continue
                    bar.update(1)
                    n_done += 1
                    if n_done % SAVE_INTERVAL == 0:
                        _flush()
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

    _flush()  # final flush
    return unresolved


# ── npm registry deps ─────────────────────────────────────────────────────────

async def fetch_deps_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    package: str,
    bar: tqdm | None = None,
) -> tuple[str, list[tuple[str, str]] | None]:
    """Return (package, deps). deps is None when retries were exhausted —
    never treat that the same as a package confirmed to have zero deps."""
    url = f"{NPM_REGISTRY}/{package}/latest"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 404:
                        return package, []
                    if r.status == 429:
                        wait = _backoff_wait(attempt, r.headers.get("Retry-After"))
                        rl.pause(wait)  # hold ALL tasks — don't let others probe the same window
                        if bar: bar.set_description(f"  deps [rate limit — waiting {wait:.0f}s]")
                        await asyncio.sleep(wait)
                        if bar: bar.set_description("  deps")
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    return package, list((data.get("dependencies") or {}).items())
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    wait = _backoff_wait(attempt)
                    if bar: bar.set_description(f"  deps [retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s]")
                    await asyncio.sleep(wait)
                    if bar: bar.set_description("  deps")
    return package, None  # retries exhausted — unresolved, retried next round


def _registry_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if NPM_TOKEN:
        headers["Authorization"] = f"Bearer {NPM_TOKEN}"
    return headers


async def fetch_all_deps(
    packages: list[str], concurrency: int, bar: tqdm | None = None
) -> dict[str, list[tuple[str, str]] | None]:
    sem = asyncio.Semaphore(concurrency)
    rl  = RateLimiter(RATE_PER_SEC)
    async with aiohttp.ClientSession(
        headers=_registry_headers(),
        cookie_jar=aiohttp.DummyCookieJar(),  # see downloads session — don't carry a flagged _cfuvid
    ) as session:
        futs = [asyncio.ensure_future(fetch_deps_one(session, sem, rl, p, bar)) for p in packages]
        results = []
        for fut in asyncio.as_completed(futs):
            results.append(await fut)
            if bar:
                bar.update(1)
    return dict(results)


# ── fetch + save helpers (used by process_data.py) ────────────────────────────

def fetch_and_save_deps(packages: list[str], concurrency: int = CONCURRENCY) -> int:
    """Fetch runtime deps for packages, upsert into raw/deps every 20. Returns new edge count."""
    raw_deps   = load_raw_deps()
    edge_count = 0
    with tqdm(total=len(packages), desc="  deps", unit="pkg") as bar:
        for i in range(0, len(packages), 20):
            chunk     = packages[i:i + 20]
            deps_data = asyncio.run(fetch_all_deps(chunk, concurrency, bar=bar))
            now       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
            for pkg, deps in deps_data.items():
                if deps is None:
                    continue  # retries exhausted — leave unfetched, retried next round
                if deps:
                    raw_deps[pkg] = [(dep_name, dep_ver, now) for dep_name, dep_ver in deps if dep_name]
                    edge_count += len(raw_deps[pkg])
                else:
                    raw_deps[pkg] = [("__none__", "", now)]
            write_raw_deps(raw_deps)
    return edge_count


def fetch_and_save_downloads(packages: list[str], concurrency: int = CONCURRENCY) -> int:
    """One asyncio.run(), one session. Returns the unresolved-package count."""
    raw_dl = load_raw_downloads()
    with tqdm(total=len(packages), desc="  downloads", unit="pkg") as bar:
        return asyncio.run(_fetch_downloads_async(packages, concurrency, raw_dl, bar))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds",  type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--limit",       type=int, default=None, help="Max packages per step (test mode)")
    args = parser.parse_args()

    console.rule("[bold]npm — fetch_npm_data")
    console.print(f"  Max rounds  : {args.max_rounds}")
    console.print(f"  Concurrency : {args.concurrency}")
    if args.limit:
        console.print(f"  Limit       : [yellow]{args.limit}[/yellow] (test mode)")
    console.print()

    t0 = time.perf_counter()

    for round_num in range(1, args.max_rounds + 1):
        console.rule(f"[bold]Round {round_num}")

        raw_dl        = load_raw_downloads()
        known         = packages_with_all_years(raw_dl)
        fetched_deps  = load_fetched_dep_packages()
        all_dep_names = load_all_dep_names()
        missing_dl    = all_dep_names - known
        need_deps     = known - fetched_deps

        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row("Known packages",    f"{len(known):,}")
        tbl.add_row("Missing downloads", f"[yellow]{len(missing_dl):,}[/yellow]" if missing_dl else "[green]0[/green]")
        tbl.add_row("Need dep fetch",    f"[yellow]{len(need_deps):,}[/yellow]"  if need_deps  else "[green]0[/green]")
        console.print(tbl)

        if not missing_dl and not need_deps:
            console.print("[bold green]Graph is complete.[/bold green]")
            break

        if missing_dl:
            pkgs = sorted(missing_dl)[:args.limit] if args.limit else sorted(missing_dl)
            console.print(f"\n  Fetching downloads for {len(pkgs):,} packages …")
            t = time.perf_counter()
            unresolved = fetch_and_save_downloads(pkgs, args.concurrency)
            console.print(
                f"  Done ({time.perf_counter()-t:.1f}s)"
                + (f" — [yellow]{unresolved} unresolved (retries exhausted)[/yellow]"
                   if unresolved else "")
            )
            if unresolved:
                # npm's rolling-window throttle caused the exhaustion; barging
                # straight into the next round just re-triggers it.
                console.print("  [yellow]cooling down 60s before continuing[/yellow]")
                time.sleep(60)

        if need_deps:
            pkgs = sorted(need_deps)[:args.limit] if args.limit else sorted(need_deps)
            console.print(f"\n  Fetching deps for {len(pkgs):,} packages …")
            t = time.perf_counter()
            new_edges = fetch_and_save_deps(pkgs, args.concurrency)
            console.print(f"  Added {new_edges:,} dep edges ({time.perf_counter()-t:.1f}s)")

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
