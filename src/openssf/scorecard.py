"""Fetch OpenSSF Scorecard results and upsert them into two output files.

Outputs:
    data/openssf-data.json   — full API response per repo (keyed by owner/repo)
    data/openssf-score.csv   — summary: repo, score, checked_at

Usage:
    # Single repo
    uv run src/openssf/scorecard.py owner/repo

    # Multiple repos
    uv run src/openssf/scorecard.py owner/repo1 owner/repo2

    # Batch from file (one "owner/repo" per line, or a JSON array of strings)
    uv run src/openssf/scorecard.py --file repos.txt

    # Set concurrency limit (default: 10)
    uv run src/openssf/scorecard.py --file repos.txt --concurrency 5
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

log = logging.getLogger(__name__)
console = Console()

DEFAULT_DATA_OUTPUT = Path("data/openssf/data.json")
DEFAULT_CSV_OUTPUT = Path("data/openssf/scores.csv")


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


# ---------------------------------------------------------------------------
# CLI fetching
# ---------------------------------------------------------------------------


def _scorecard_bin() -> str:
    path = shutil.which("scorecard")
    if not path:
        raise RuntimeError("scorecard CLI not found — install with: brew install scorecard")
    return path


def _github_token() -> str:
    token = os.environ.get("GITHUB_AUTH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        import subprocess as _sp
        try:
            token = _sp.check_output(["gh", "auth", "token"], text=True).strip()
        except Exception:
            pass
    if not token:
        raise RuntimeError("No GitHub token found. Set GITHUB_AUTH_TOKEN or run: gh auth login")
    return token


SUBPROCESS_TIMEOUT_S = 300  # 5 min per repo — scorecard internal retries
                            # can otherwise wedge a worker for 7+ minutes


def run_scorecard(repo: str, token: str) -> dict:
    """Run the scorecard CLI for a single repo and return parsed JSON.

    Wraps the subprocess in a hard timeout so we don't get stuck on
    scorecard's internal rate-limit / 504 retry loops (known to spin
    for 7+ min per call). On timeout or non-zero exit we record an
    error row and move on.
    """
    try:
        result = subprocess.run(
            [_scorecard_bin(), "--repo", repo, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            env={**os.environ, "GITHUB_AUTH_TOKEN": token},
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


async def fetch_all(repos: list[str], concurrency: int = 5) -> dict[str, dict]:
    """Run scorecard CLI for all repos with bounded concurrency.

    Upserts each result to disk as soon as it completes.
    Returns a mapping of repo -> full scorecard result.
    """
    token = _github_token()
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    completed = 0
    total = len(repos)

    async def bounded_run(repo: str) -> None:
        nonlocal completed
        async with semaphore:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, run_scorecard, repo, token)
            results[repo] = data
            upsert_json(DEFAULT_DATA_OUTPUT, {repo: data})
            upsert_csv(DEFAULT_CSV_OUTPUT, {repo: data})
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
        await asyncio.gather(*(bounded_run(repo) for repo in repos))

    return results


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def upsert_json(data_path: Path, new_results: dict[str, dict]) -> dict[str, dict]:
    """Upsert full scorecard responses into the JSON file (keyed by owner/repo)."""
    existing: dict[str, dict] = {}
    if data_path.exists():
        existing = json.loads(data_path.read_text())

    for repo, data in new_results.items():
        if data.get("error"):
            console.print(f"  [yellow]⚠ Skipping {repo}: {data.get('message', 'unknown error')[:100]}[/yellow]")
            continue
        existing[repo] = data

    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(existing, indent=2) + "\n")
    return existing


def upsert_csv(csv_path: Path, new_results: dict[str, dict]) -> None:
    """Upsert repo/score/checked_at rows into the CSV file."""
    existing: dict[str, dict] = {}
    if csv_path.exists():
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                existing[row["repo"]] = row

    for repo, data in new_results.items():
        if data.get("error"):
            continue
        existing[repo] = {"repo": repo, "score": data.get("score", ""), "checked_at": str(data.get("date", ""))[:10]}

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["repo", "score", "checked_at"])
        writer.writeheader()
        writer.writerows(sorted(existing.values(), key=lambda r: r["repo"]))


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


def load_already_scored(csv_path: Path) -> set[str]:
    """Return the set of repos already present in scores.csv."""
    if not csv_path.exists():
        return set()
    with csv_path.open() as f:
        return {row["repo"] for row in csv.DictReader(f)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and upsert OpenSSF Scorecard scores.")
    parser.add_argument("repos", nargs="*", help="One or more owner/repo identifiers")
    parser.add_argument("--file", "-f", type=Path, help="File with repo list (one per line or JSON array)")
    parser.add_argument("--concurrency", "-c", type=int, default=10, help="Max concurrent API requests")
    parser.add_argument("--force", action="store_true", help="Re-scan repos even if already in scores.csv")
    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    repos: list[str] = list(args.repos) if args.repos else []
    if args.file:
        repos.extend(load_repos_from_file(args.file))

    if not repos:
        parser.error("Provide at least one repo via positional args or --file")

    repos = list(dict.fromkeys(repos))  # deduplicate, preserve order

    if not args.force:
        already_scored = load_already_scored(DEFAULT_CSV_OUTPUT)
        skipped = [r for r in repos if r in already_scored]
        repos = [r for r in repos if r not in already_scored]
        if skipped:
            console.print(f"[dim]Skipping {len(skipped)} already-scored repo(s). Use --force to rescan.[/dim]")
        if not repos:
            console.print("[green]All repos already scored — nothing to do.[/green]")
            return

    console.print(f"[bold]Fetching OpenSSF Scorecards for {len(repos)} repo(s)...[/bold]\n")

    results = await fetch_all(repos, concurrency=args.concurrency)

    display_summary(results)
    console.print(f"\n[green]Full data → {DEFAULT_DATA_OUTPUT}[/green]")
    console.print(f"[green]Scores    → {DEFAULT_CSV_OUTPUT}[/green]")


if __name__ == "__main__":
    asyncio.run(main())
