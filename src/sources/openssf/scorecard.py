"""Fetch OpenSSF Scorecard results and upsert them into two output files.

Outputs:
    data/sources/openssf/data.json  — full API response per repo (keyed by owner/repo).
                              Canonical raw cache; preserved as-is.
    data/sources/git/openssf.csv    — long-format sha-pinned snapshot rows
                              (repo, repo_id, commit_sha, metric, value, checked_at).
                              One row per check (`binary_artifacts`, `ci_tests`, …)
                              plus one for the overall `score`.

`data/sources/git/openssf.csv` is the canonical sha-pinned long-format output;
the legacy wide `data/sources/openssf/scores.csv` summary has been removed.

Usage:
    # Single repo
    uv run src/sources/openssf/scorecard.py owner/repo

    # Multiple repos
    uv run src/sources/openssf/scorecard.py owner/repo1 owner/repo2

    # Batch from file (one "owner/repo" per line, or a JSON array of strings)
    uv run src/sources/openssf/scorecard.py --file repos.txt

    # All risk repos (A/B value-class, from data/value/value.csv) — default when no args
    uv run src/sources/openssf/scorecard.py

    # Set concurrency limit (default: 10)
    uv run src/sources/openssf/scorecard.py --file repos.txt --concurrency 5
"""

import argparse
import asyncio
import csv
import functools
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from src.common.repos import load_github_top_slugs
from src.common.repos import load_repo_ids as load_repo_id_map
from src.sources.git.long_format import _file_lock, upsert_snapshot
from src.sources.git.long_format import read as read_long

log = logging.getLogger(__name__)
console = Console()

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_OUTPUT = ROOT / "data" / "sources" / "openssf" / "data.json"
DEFAULT_LONG_OUTPUT = ROOT / "data" / "sources" / "git" / "openssf.csv"
GITLAB_PROJECTS = ROOT / "data" / "sources" / "gitlab" / "repos.csv"


# ---------------------------------------------------------------------------
# Repo list loading
# ---------------------------------------------------------------------------


def load_repos_from_file(path: Path) -> list[str]:
    """Load repos from a text file (one per line) or a JSON array."""
    text = path.read_text().strip()
    if text.startswith("["):
        repos = json.loads(text)
        if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
            raise ValueError(f"{path} must contain a JSON array of strings")
        return repos
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def to_snake_case(name: str) -> str:
    """`Binary-Artifacts` → `binary_artifacts`, `CII-Best-Practices` → `cii_best_practices`.

    Hyphens become underscores; everything is lowercased. We don't try to
    insert separators inside CamelCase tokens because Scorecard check names
    are already hyphen-separated (e.g. `SAST`, `CII-Best-Practices`).
    """
    s = name.strip().replace("-", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.lower()


# ---------------------------------------------------------------------------
# CLI fetching
# ---------------------------------------------------------------------------


def _scorecard_bin() -> str:
    path = shutil.which("scorecard")
    if not path:
        raise RuntimeError("scorecard CLI not found — install with: brew install scorecard")
    return path


def _github_tokens() -> list[str]:
    """Load all GitHub tokens for round-robin across concurrent subprocesses.

    Falls back to a single token from GITHUB_AUTH_TOKEN/GITHUB_TOKEN/gh-cli
    if GITHUB_TOKENS isn't set, so single-token setups still work. The
    multi-token case is what makes concurrent scorecard runs survive
    GitHub rate limits — each subprocess gets its own quota."""
    # Load .env into the process environment first
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    tokens_csv = os.environ.get("GITHUB_TOKENS", "").strip()
    if tokens_csv:
        tokens = [t.strip() for t in tokens_csv.split(",") if t.strip()]
        if tokens:
            return tokens

    single = os.environ.get("GITHUB_AUTH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not single:
        import subprocess as _sp
        try:
            single = _sp.check_output(["gh", "auth", "token"], text=True).strip()
        except Exception:
            single = None
    if not single:
        raise RuntimeError(
            "No GitHub token found. Set GITHUB_TOKENS (comma-separated, "
            "preferred for concurrency), GITHUB_AUTH_TOKEN, or run: gh auth login"
        )
    return [single]


SUBPROCESS_TIMEOUT_S = 300  # 5 min per repo — scorecard internal retries
                            # can otherwise wedge a worker for 7+ minutes

# Checks that work without org-admin / branch-protection PAT scope.
# Branch-Protection is intentionally excluded — it requires admin:repo
# privileges that personal/org tokens typically don't have, and its
# absence was causing the scorecard CLI to exit non-zero, so we'd lose
# the entire run for that repo. Override with SCORECARD_CHECKS env var
# to customize the check list.
SCORECARD_CHECKS = os.environ.get("SCORECARD_CHECKS", ",".join([
    "Binary-Artifacts",
    "CI-Tests",
    "CII-Best-Practices",
    "Code-Review",
    "Contributors",
    "Dangerous-Workflow",
    "Dependency-Update-Tool",
    "Fuzzing",
    "License",
    "Maintained",
    "Packaging",
    "Pinned-Dependencies",
    "SAST",
    "Security-Policy",
    "Signed-Releases",
    "Token-Permissions",
    "Vulnerabilities",
    # NOTE: "Webhooks" is in the user spec but NOT supported by the
    # installed scorecard CLI's --checks flag — including it makes
    # the CLI exit non-zero with "invalid check name". Omitted.
    # NOTE: "Branch-Protection" intentionally omitted — needs admin scope.
]))


# Scorecard checks applicable to GitLab. The GitHub-Actions/statuses specific
# checks are excluded: CI-Tests errors (commit-statuses 404 on GitLab), and
# Dangerous-Workflow / Packaging / Pinned-Dependencies / Signed-Releases /
# Token-Permissions are Actions-only (return inconclusive). Branch-Protection
# is omitted for the same admin-scope reason as GitHub. SAST is kept even
# though it 403s on the pipelines API for many repos — that per-repo error
# makes the CLI exit non-zero but the aggregate is still computed, and the
# GitLab path's tolerate_partial recovers it (so we keep the signal where
# SAST *does* resolve rather than dropping the check for everyone).
GITLAB_SCORECARD_CHECKS = os.environ.get("GITLAB_SCORECARD_CHECKS", ",".join([
    "Binary-Artifacts",
    "CII-Best-Practices",
    "Code-Review",
    "Contributors",
    "Dependency-Update-Tool",
    "Fuzzing",
    "License",
    "Maintained",
    "SAST",
    "Security-Policy",
    "Vulnerabilities",
]))


# Hosts whose *anonymous* GitLab API is rate-limited hard enough that Scorecard
# wedges on internal retries — score these only when we hold a token. Every
# other GitLab instance (salsa.debian.org, gitlab.gnome.org, gitlab.freedesktop.org,
# invent.kde.org, …) serves its REST API anonymously, so Scorecard scores it
# token-free: a few auth-only checks (Maintained, Contributors, Dependency-Update-Tool)
# come back inconclusive, but the aggregate is still computed and tolerate_partial
# keeps it. gitlab.com is the lone exception because its unauthenticated quota is
# too small for Scorecard's call volume.
SCORECARD_ANON_UNRELIABLE = {"gitlab.com"}


def run_scorecard(repo: str, token: str, *,
                  token_env: str = "GITHUB_AUTH_TOKEN",
                  checks: str = SCORECARD_CHECKS,
                  tolerate_partial: bool = False) -> dict:
    """Run the scorecard CLI for a single repo and return parsed JSON.

    Wraps the subprocess in a hard timeout so we don't get stuck on
    scorecard's internal rate-limit / 504 retry loops (known to spin
    for 7+ min per call). On timeout or non-zero exit we record an
    error row and move on.

    ``token_env`` is the env var the token is passed through — ``GITHUB_AUTH_TOKEN``
    for github.com repos, ``GITLAB_AUTH_TOKEN`` for ``{host}/{path}`` GitLab
    targets. ``checks`` selects the check subset (Branch-Protection is omitted
    for admin scope; GitLab uses ``GITLAB_SCORECARD_CHECKS``).
    """
    try:
        result = subprocess.run(
            [
                _scorecard_bin(),
                "--repo", repo,
                "--format", "json",
                f"--checks={checks}",
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            env={**os.environ, token_env: token or ""},
        )
    except subprocess.TimeoutExpired:
        log.warning("scorecard timed out for %s after %ds", repo, SUBPROCESS_TIMEOUT_S)
        return {"error": True, "repo": repo,
                "message": f"timed out after {SUBPROCESS_TIMEOUT_S}s"}
    except Exception as exc:
        log.error("scorecard error for %s: %s", repo, exc)
        return {"error": True, "repo": repo, "message": str(exc)}

    if result.returncode != 0:
        msg = result.stderr.strip()[:500]
        # Scorecard exits non-zero if ANY single check hits an internal error,
        # but with --format json it still emits the full result (the aggregate
        # score is computed from the checks that succeeded). On GitLab a
        # per-repo permission gap on one endpoint (e.g. SAST's 403 on the
        # pipelines API) is common and shouldn't cost us the whole repo, so the
        # GitLab path recovers the partial-but-valid JSON from stdout.
        if tolerate_partial and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and (
                data.get("score") is not None or data.get("checks")
            ):
                log.info("scorecard partial for %s (a check errored): %s",
                         repo, msg[:160])
                return data
        log.warning("scorecard failed for %s: %s", repo, msg)
        return {"error": True, "repo": repo, "message": msg}
    if not result.stdout.strip():
        log.warning("scorecard returned empty stdout for %s", repo)
        return {"error": True, "repo": repo, "message": "empty stdout"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("scorecard returned non-JSON for %s: %s", repo, exc)
        return {"error": True, "repo": repo, "message": f"non-JSON output: {exc}"}


async def fetch_all(
    repos: list[str],
    concurrency: int = 5,
    repo_ids: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Run scorecard CLI for all repos with bounded concurrency.

    Upserts each result to disk as soon as it completes:
      - data/sources/openssf/data.json (raw cache, keyed by owner/repo)
      - data/sources/git/openssf.csv (long-format sha-pinned metric rows)

    Returns a mapping of repo -> full scorecard result.

    Tokens are round-robined across calls so concurrent scorecard
    subprocesses don't all share one rate-limit budget.
    """
    tokens = _github_tokens()
    repo_ids = repo_ids or {}
    console.print(f"[dim]Using {len(tokens)} GitHub token(s); concurrency={concurrency}[/dim]")
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    completed = 0
    total = len(repos)

    async def bounded_run(repo: str, idx: int) -> None:
        nonlocal completed
        async with semaphore:
            token = tokens[idx % len(tokens)]
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, run_scorecard, repo, token)
            results[repo] = data
            upsert_json(DEFAULT_DATA_OUTPUT, {repo: data})
            upsert_long(DEFAULT_LONG_OUTPUT, {repo: data}, repo_ids=repo_ids)
            completed += 1
            progress.advance(task)
            # Periodic stdout flush so background runs show progress without
            # depending on rich's TTY-only progress bar
            if completed % 25 == 0 or completed == total:
                tag = "ok" if not data.get("error") else "err"
                console.print(
                    f"[dim]{completed}/{total} ({tag}: {repo})[/dim]"
                )

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning repos", total=total)
        await asyncio.gather(*(bounded_run(repo, i) for i, repo in enumerate(repos)))

    return results


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


def load_gitlab_targets(hosts: set[str] | None = None,
                        classes: set[str] | None = None) -> list[dict]:
    """Valid GitLab projects from data/sources/gitlab/repos.csv.

    Returns ``[{project, host, repo_id}]`` where ``project`` = ``"{host}/{path}"``
    is a valid Scorecard ``--repo`` target (e.g. ``gitlab.com/gnutls/gnutls``)
    and ``repo_id`` = ``gl/{nickname}-{id}`` (bare ``gl/{id}`` for gitlab.com; see
    ``gitlab_client.make_repo_id``). Optionally filter to ``hosts``.

    ``classes`` (e.g. ``{"A"}``) scopes to those value classes by joining to
    ``value.csv`` on the project key — repos.csv has no class column, so the
    class comes from the same value.csv the fetcher scopes on. A project absent
    from that class set (e.g. a class-C row that leaked into repos.csv) is
    dropped.
    """
    out: list[dict] = []
    if not GITLAB_PROJECTS.exists():
        return out
    allowed: set[str] | None = None
    if classes:
        from src.sources.gitlab.fetch_project_data import load_gitlab_rows
        allowed = {r["project"] for r in load_gitlab_rows(classes=classes)}
    with open(GITLAB_PROJECTS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("valid") or "") != "True" or not r.get("repo_id"):
                continue
            if hosts and r.get("host") not in hosts:
                continue
            if allowed is not None and r["project"] not in allowed:
                continue
            out.append({"project": r["project"], "host": r["host"],
                        "repo_id": r["repo_id"]})
    return out


async def fetch_all_gitlab(
    targets: list[dict],
    token_map: dict[str, str],
    concurrency: int = 5,
) -> dict[str, dict]:
    """Run Scorecard on GitLab targets, keyed on the ``gl/{nickname}-{id}`` repo_id.

    Each subprocess gets its host's token via ``GITLAB_AUTH_TOKEN`` and the
    GitLab check subset. Upserts to the same ``data.json`` + ``openssf.csv``
    outputs as the GitHub path (long rows carry the ``gl/`` repo_id).
    """
    repo_ids = {t["project"]: t["repo_id"] for t in targets}
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}
    completed = 0
    total = len(targets)

    async def bounded_run(t: dict) -> None:
        nonlocal completed
        async with semaphore:
            token = token_map.get(t["host"], "")
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                functools.partial(run_scorecard, t["project"], token,
                                  token_env="GITLAB_AUTH_TOKEN",
                                  checks=GITLAB_SCORECARD_CHECKS,
                                  tolerate_partial=True),
            )
            results[t["project"]] = data
            upsert_json(DEFAULT_DATA_OUTPUT, {t["project"]: data})
            upsert_long(DEFAULT_LONG_OUTPUT, {t["project"]: data}, repo_ids=repo_ids)
            completed += 1
            progress.advance(task)
            if completed % 25 == 0 or completed == total:
                tag = "ok" if not data.get("error") else "err"
                console.print(f"[dim]{completed}/{total} ({tag}: {t['project']})[/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning GitLab repos", total=total)
        await asyncio.gather(*(bounded_run(t) for t in targets))

    return results


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def upsert_json(data_path: Path, new_results: dict[str, dict]) -> dict[str, dict]:
    """Upsert full scorecard responses into the JSON file (keyed by owner/repo).

    The read-merge-write runs under the same inter-process lock the
    long-format CSV uses, so two concurrent scorecard runs (e.g. a GitHub
    scan and a ``--gitlab`` scan in separate terminals) writing this shared
    ``data.json`` can't lose each other's just-scored repos.
    """
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(data_path):
        existing: dict[str, dict] = {}
        if data_path.exists():
            existing = json.loads(data_path.read_text())

        for repo, data in new_results.items():
            if data.get("error"):
                console.print(f"  [yellow]⚠ Skipping {repo}: {data.get('message', 'unknown error')[:100]}[/yellow]")
                continue
            existing[repo] = data

        data_path.write_text(json.dumps(existing, indent=2) + "\n")
    return existing


def upsert_long(
    long_path: Path,
    new_results: dict[str, dict],
    repo_ids: dict[str, str] | None = None,
) -> None:
    """Upsert each successful scorecard result as long-format rows.

    Writes one row per check + one row for the overall `score`, all keyed
    by (repo, commit_sha, metric). Skips errored results and any result
    missing `repo.commit` (we can't pin metrics without a sha).
    """
    repo_ids = repo_ids or {}
    for repo, data in new_results.items():
        if data.get("error"):
            continue
        commit_sha = (data.get("repo") or {}).get("commit") or ""
        if not commit_sha:
            log.warning("no commit sha for %s — skipping long-format upsert", repo)
            continue
        checks = data.get("checks") or []
        metrics: dict[str, object] = {"score": data.get("score")}
        for c in checks:
            name = c.get("name")
            if not name:
                continue
            metrics[to_snake_case(name)] = c.get("score")
        upsert_snapshot(
            long_path,
            repo=repo,
            repo_id=repo_ids.get(repo, ""),
            commit_sha=commit_sha,
            metrics=metrics,
            checked_at=str(data.get("date") or ""),
        )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def display_summary(results: dict[str, dict]) -> None:
    """Print a summary table of fetched scores."""
    table = Table(title="OpenSSF Scorecard Results")
    table.add_column("Repository", style="cyan")
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Checks Passed", justify="right")
    table.add_column("Date", style="dim")

    for repo, data in sorted(results.items()):
        if data.get("error"):
            table.add_row(repo, "[red]ERROR[/red]", "-", "-")
            continue
        score = data.get("score", "?")
        checks = data.get("checks", [])
        passed = sum(1 for c in checks if c.get("score", 0) >= 7)
        date = data.get("date", data.get("scorecard", {}).get("commit", "?"))
        table.add_row(repo, f"{score}", f"{passed}/{len(checks)}", str(date)[:10])

    console.print(table)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_already_scored(long_path: Path) -> set[str]:
    """Return repos that already have a `score` row in the long-format CSV.

    A repo is considered "scored" if any (repo, sha, score) row exists —
    we don't try to determine sha freshness here. Use --force to rescan.
    """
    rows = read_long(long_path)
    return {repo for (repo, _sha, metric) in rows.keys() if metric == "score"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and upsert OpenSSF Scorecard scores for risk repos "
            "(A/B value-class from data/value/value.csv). "
            "Writes raw JSON to data/sources/openssf/data.json and long-format rows "
            "to data/sources/git/openssf.csv."
        ),
    )
    parser.add_argument("repos", nargs="*", help="One or more owner/repo identifiers (default: all risk repos)")
    parser.add_argument("--file", "-f", type=Path, help="File with repo list (one per line or JSON array)")
    parser.add_argument("--concurrency", "-c", type=int, default=10, help="Max concurrent API requests")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scan repos even if they already have a score row in data/sources/git/openssf.csv",
    )
    parser.add_argument(
        "--gitlab",
        action="store_true",
        help="Score GitLab projects from data/sources/gitlab/repos.csv using "
             "per-host GITLAB tokens (GITLAB_TOKENS). Output keyed on gl/{nickname}-{id}.",
    )
    parser.add_argument(
        "--host",
        nargs="+",
        default=None,
        help="With --gitlab: limit to these GitLab hosts (e.g. --host gitlab.com).",
    )
    parser.add_argument(
        "--classes",
        nargs="+",
        default=None,
        help="With --gitlab: limit to these value classes (e.g. --classes A). "
             "Default: every valid project in repos.csv.",
    )
    return parser


async def _run_gitlab(args: argparse.Namespace,
                      only_repo_ids: set[str] | None = None) -> None:
    """Score GitLab projects (per-host tokens) → same data.json + openssf.csv.

    `only_repo_ids` restricts targets to those repo_ids — the pipeline passes
    the top-scope gl/ ids so a bare run scores ONLY what risk.csv needs
    (same lean-fetch discipline as the GitHub pass), never the whole
    gitlab/repos.csv backlog. Explicit --gitlab runs stay unrestricted.
    """
    from src.sources.gitlab.gitlab_client import load_token_map

    if args.repos or args.file:
        console.print("[yellow]--gitlab ignores positional repos / --file; "
                      "targets come from data/sources/gitlab/repos.csv.[/yellow]")

    hosts = set(args.host) if args.host else None
    classes = set(args.classes) if args.classes else None
    targets = load_gitlab_targets(hosts, classes)
    if only_repo_ids is not None:
        targets = [t for t in targets if t["repo_id"] in only_repo_ids]
    token_map = load_token_map()

    # A token gets a host more checks + higher rate limits, but self-hosted
    # instances serve their API anonymously, so a tokenless host is scored
    # anonymously rather than skipped. Only SCORECARD_ANON_UNRELIABLE hosts
    # (gitlab.com) are dropped when we have no token for them.
    present = {t["host"] for t in targets}
    tokenless = {h for h in present if not token_map.get(h)}
    skip = tokenless & SCORECARD_ANON_UNRELIABLE
    anon = tokenless - skip
    targets = [t for t in targets if t["host"] not in skip]
    if skip:
        console.print(f"[yellow]No GITLAB token for {sorted(skip)} and its "
                      f"anonymous API is rate-limited — skipping (set GITLAB_TOKENS).[/yellow]")
    if anon:
        console.print(f"[dim]Scoring {sorted(anon)} anonymously (no token; a few "
                      f"auth-only checks come back inconclusive).[/dim]")

    if not args.force:
        scored = load_already_scored(DEFAULT_LONG_OUTPUT)
        before = len(targets)
        targets = [t for t in targets if t["project"] not in scored]
        if before - len(targets):
            console.print(f"[dim]Skipping {before - len(targets)} already-scored "
                          f"GitLab project(s). Use --force to rescan.[/dim]")

    if not targets:
        console.print("[green]No GitLab targets to score (all scored, or no token).[/green]")
        return

    console.print(f"[bold]Scoring {len(targets)} GitLab project(s) "
                  f"across {len({t['host'] for t in targets})} instance(s)...[/bold]\n")
    results = await fetch_all_gitlab(targets, token_map, concurrency=args.concurrency)
    display_summary(results)
    console.print(f"\n[green]Raw JSON  → {DEFAULT_DATA_OUTPUT}[/green]")
    console.print(f"[green]Long CSV  → {DEFAULT_LONG_OUTPUT}[/green]")


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.gitlab:
        await _run_gitlab(args)
        return

    repos: list[str] = list(args.repos) if args.repos else []
    if args.file:
        repos.extend(load_repos_from_file(args.file))

    if not repos:
        # GitHub-only list: the default (non---gitlab) mode scans github.com/<slug>,
        # so a GitLab slug here would silently score an unrelated GitHub repo.
        repos = load_github_top_slugs()
        if not repos:
            parser.error("No repos found — provide positional args, --file, or populate data/value/value.csv")

    repos = list(dict.fromkeys(repos))  # deduplicate, preserve order

    if not args.force:
        already_scored = load_already_scored(DEFAULT_LONG_OUTPUT)
        skipped = [r for r in repos if r in already_scored]
        repos = [r for r in repos if r not in already_scored]
        if skipped:
            console.print(f"[dim]Skipping {len(skipped)} already-scored repo(s). Use --force to rescan.[/dim]")
        if not repos:
            console.print("[green]All repos already scored — nothing to do.[/green]")
            return

    repo_ids = load_repo_id_map()

    console.print(f"[bold]Fetching OpenSSF Scorecards for {len(repos)} risk repo(s)...[/bold]\n")

    results = await fetch_all(repos, concurrency=args.concurrency, repo_ids=repo_ids)

    display_summary(results)
    console.print(f"\n[green]Raw JSON  → {DEFAULT_DATA_OUTPUT}[/green]")
    console.print(f"[green]Long CSV  → {DEFAULT_LONG_OUTPUT}[/green]")

    # The risk scope includes GitLab-hosted repos, and the pipeline invokes
    # this module bare — without this, GitLab repos silently never get an
    # openssf_score (16 blank salsa/inria/freedesktop rows observed). Scoped
    # to the TOP repos' gl/ ids only (lean-fetch discipline: a bare run
    # fetches exactly what risk.csv needs); its already-scored skip keeps
    # repeat runs incremental. Full-backlog scoring stays manual: --gitlab.
    if not args.repos and not args.file:
        from src.common.repos import load_top_repos
        top_gl_ids = {e.repo_id for e in load_top_repos(skip_archived=False)
                      if e.repo_id.startswith("gl/")}
        if top_gl_ids:
            console.print("\n[bold]GitLab pass (top-scope only)[/bold]")
            await _run_gitlab(args, only_repo_ids=top_gl_ids)


if __name__ == "__main__":
    asyncio.run(main())
