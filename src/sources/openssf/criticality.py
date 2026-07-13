"""Fetch the OpenSSF **Criticality Score** for the top repos.

This is the *importance/criticality* metric from
[`ossf/criticality_score`](https://github.com/ossf/criticality_score) — distinct
from the OpenSSF **Scorecard** (security posture) fetched by `scorecard.py`. The
score (`default_score`, 0–1) follows the Rob Pike `original_pike` algorithm: a
weighted blend of project age, recency, contributor/org counts, commit/release
cadence, issue activity, and a dependents proxy.

We run the `criticality_score` Go binary in batch with `-depsdev-disable`, so no
GCP/BigQuery credentials are needed. In that mode the dependents signal is
`github_mention_count` (mentions in commit messages — the original Pike
dependents proxy), not the deps.dev dependent count. To use the precise
deps.dev dependents instead, pass `--gcp-project <id>` (requires
`gcloud auth login --update-adc`); the `depsdev_enabled` column records which
mode produced each row.

Output (wide, one row per repo): ``data/sources/openssf/criticality.csv``.
Every row carries `checked_at` (UTC ISO 8601), a `status` flag, and an
`error_reason`, so a `0`/empty value is distinguishable from a failed/never-run
fetch. A 1-year TTL skips repos already fetched within the last 365 days
(override with ``--force``).

Known structural gap (`error_reason=unresolvable-contributor`): the tool derives
`org_count` by GraphQL-querying each top contributor's company, and a non-user
contributor login fails the whole repo with `Could not resolve to a User`. The
dominant case is GitHub's **"Copilot"** coding-agent identity appearing as a
recent contributor — an upstream `criticality_score` v2.0.4 bug, not a fetch we
can retry. These repos fail deterministically (no backoff-retry) and are the
bulk of the sub-100% coverage; see [the preview stats sheet] for the count.

Install the binary once:
    go install github.com/ossf/criticality_score/v2/cmd/criticality_score@latest

Usage:
    # All top repos (valid class-A from data/value/value.csv, archived
    # included); TTL-skips fresh rows
    uv run python -m src.sources.openssf.criticality

    # Specific repos
    uv run python -m src.sources.openssf.criticality owner/repo1 owner/repo2

    # Ignore TTL / tune throughput
    uv run python -m src.sources.openssf.criticality --force --workers 16 --chunk 50
"""

import argparse
import csv
import datetime as dt
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import VALUE_FILE, load_repo_ids, load_top_slugs
from src.sources.openssf.scorecard import _github_tokens

log = logging.getLogger(__name__)
console = Console()

ROOT = Path(__file__).resolve().parents[3]
OUTPUT_FILE = ROOT / "data" / "sources" / "openssf" / "criticality.csv"

TTL_DAYS = 365  # criticality moves slowly; refresh at most yearly
# The score's dependents proxy (`github_mention_count`) is fetched via GitHub's
# `search/commits` endpoint, which trips a strict SECONDARY rate limit under
# concurrency/burst — and the binary treats that 403 as a fatal per-repo error
# that aborts the whole batch. So we run one repo per invocation (failures stay
# isolated) at workers=1, throttle between repos to keep search/commits well
# under its ~30/min limit, and retry transient rate-limit failures with backoff.
CHUNK_DEFAULT = 1
WORKERS_DEFAULT = 1
THROTTLE_DEFAULT = 2.5  # seconds between repos — keeps search/commits ~< 20/min
RETRIES_DEFAULT = 3     # per-repo retries on a (secondary) rate-limit failure
BACKOFF_BASE_S = 45     # secondary limits want "a few minutes": 45s, 90s, 135s
SUBPROCESS_TIMEOUT_S = 1800  # 30 min per repo — big repos page many contributors

# Our wide schema. `criticality_score` is the tool's `default_score`; the raw
# signals are the tool's `legacy.*` columns; identity + audit columns are ours.
FIELDS = [
    "repo", "repo_id", "criticality_score",
    "language", "license", "star_count", "created_at", "updated_at",
    "created_since", "updated_since", "contributor_count", "org_count",
    "commit_frequency", "recent_release_count", "updated_issues_count",
    "closed_issues_count", "issue_comment_frequency", "github_mention_count",
    "depsdev_enabled", "checked_at", "status", "error_reason",
]

# criticality_score CSV column -> our column.
COLMAP = {
    "default_score": "criticality_score",
    "repo.language": "language",
    "repo.license": "license",
    "repo.star_count": "star_count",
    "repo.created_at": "created_at",
    "repo.updated_at": "updated_at",
    "legacy.created_since": "created_since",
    "legacy.updated_since": "updated_since",
    "legacy.contributor_count": "contributor_count",
    "legacy.org_count": "org_count",
    "legacy.commit_frequency": "commit_frequency",
    "legacy.recent_release_count": "recent_release_count",
    "legacy.updated_issues_count": "updated_issues_count",
    "legacy.closed_issues_count": "closed_issues_count",
    "legacy.issue_comment_frequency": "issue_comment_frequency",
    "legacy.github_mention_count": "github_mention_count",
}


def _criticality_bin() -> str:
    """Resolve the criticality_score binary (PATH, then $GOPATH/bin, then ~/go/bin)."""
    found = shutil.which("criticality_score")
    if found:
        return found
    candidates = []
    gopath = os.environ.get("GOPATH")
    if gopath:
        candidates.append(Path(gopath) / "bin" / "criticality_score")
    candidates.append(Path.home() / "go" / "bin" / "criticality_score")
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError(
        "criticality_score binary not found — install with:\n"
        "  go install github.com/ossf/criticality_score/v2/cmd/criticality_score@latest"
    )


def slug_from_url(url: str) -> str:
    """`https://github.com/Owner/Repo` -> `owner/repo` (lowercased)."""
    s = (url or "").strip().rstrip("/")
    s = s.split("github.com/", 1)[-1]
    parts = [p for p in s.split("/") if p]
    return "/".join(parts[:2]).lower() if len(parts) >= 2 else ""


def _is_fresh(row: dict, ttl_days: int) -> bool:
    """True iff the row was fetched OK within ttl_days. Errors are always retried."""
    if (row.get("status") or "").strip().lower() != "ok":
        return False
    ts = (row.get("checked_at") or "").strip()
    if not ts:
        return False
    try:
        when = dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ttl_days)
    return when >= cutoff


def load_existing(path: Path) -> dict[str, dict]:
    """{repo_lower: row} from an existing criticality.csv."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            if repo:
                out[repo] = row
    return out


def _value_repo_ids(value_file: str = VALUE_FILE) -> dict[str, str]:
    """{repo slug -> bare GitHub id} from value.csv — see load_value_repo_ids."""
    from src.common.repos import load_value_repo_ids
    return load_value_repo_ids(value_file)


def _repo_id_map() -> dict[str, str]:
    """Slug -> bare GitHub id: github/repos.csv first, value.csv as fallback."""
    ids = load_repo_ids()
    for slug, rid in _value_repo_ids().items():
        ids.setdefault(slug, rid)
    return ids


def _heal_rows(rows: dict[str, dict], repo_ids: dict[str, str],
               scope: set[str]) -> int:
    """Idempotent repair of previously-written rows; runs on every invocation.

    1. Backfill an empty `repo_id` where the slug has become resolvable.
    2. Drop rows superseded by a same-repo_id row under another slug — a
       rename leaves the old-slug row behind (facebook/react error next to
       react/react ok). Keeper preference: slug in the current scope, then
       status=ok, then newest checked_at.

    Returns the number of rows changed or dropped (0 = nothing to heal).
    """
    healed = 0
    for slug, row in rows.items():
        if not (row.get("repo_id") or "").strip():
            rid = repo_ids.get(slug, "")
            if rid:
                row["repo_id"] = rid
                healed += 1

    by_id: dict[str, list[str]] = {}
    for slug, row in rows.items():
        rid = (row.get("repo_id") or "").strip()
        if rid:
            by_id.setdefault(rid, []).append(slug)
    for rid, slugs in by_id.items():
        if len(slugs) < 2:
            continue
        keeper = max(slugs, key=lambda s: (
            s in scope,
            (rows[s].get("status") or "") == "ok",
            rows[s].get("checked_at") or "",
        ))
        for s in slugs:
            if s != keeper:
                log.info("dropping stale duplicate %s (repo_id %s kept as %s)",
                         s, rid, keeper)
                del rows[s]
                healed += 1
    return healed


def _classify_failure(stderr: str, timed_out: bool) -> tuple[str, bool]:
    """Map a binary failure to (error_reason, retryable).

    Only genuinely transient failures (GitHub primary/secondary rate limits) are
    worth a backoff-retry. Deterministic failures must NOT retry-spin — otherwise
    every pipeline run burns minutes of backoff on the same repos. The dominant
    deterministic case here is `unresolvable-contributor`: criticality_score v2.0.4
    computes org_count by GraphQL-querying each top contributor's company, and a
    non-user contributor login (notably GitHub's "Copilot" coding-agent identity)
    returns NOT_FOUND, which fails the whole repo. That is an upstream tool bug, not
    a fetch we can fix by retrying.
    """
    if timed_out:
        return "timeout", False  # 30-min cap is structural; don't burn another 30 min
    s = (stderr or "").lower()
    if "secondary rate limit" in s or "exceeded a secondary" in s or "abuse" in s:
        return "rate-limit", True
    if "rate limit exceeded" in s:
        return "rate-limit", True
    if "could not resolve to a user" in s or "not_found" in s:
        return "unresolvable-contributor", False
    # Unknown errors are usually transient mid-batch rate pressure (these repos
    # succeed when retried in isolation), so retry. The one deterministic case we
    # know (unresolvable-contributor) is already handled above, so this won't
    # backoff-spin on it.
    return "error", True


def run_chunk(
    repos: list[str],
    token_str: str,
    workers: int,
    gcp_project: str | None,
    repo_ids: dict[str, str],
    now_iso: str,
) -> dict[str, dict]:
    """Run the binary on one chunk of repos; return {repo_lower: our-row}.

    Repos the tool returns no row for are marked status='error' (with an
    `error_reason` and a private `_retryable` hint) so they're retried — but
    only when the failure is transient — rather than silently dropped.
    """
    bin_path = _criticality_bin()
    urls = [f"https://github.com/{r}" for r in repos]
    depsdev_enabled = bool(gcp_project)

    with tempfile.TemporaryDirectory() as td:
        in_file = Path(td) / "repos.txt"
        out_file = Path(td) / "out.csv"
        in_file.write_text("\n".join(urls) + "\n")

        cmd = [bin_path, "-format", "csv", "-workers", str(workers),
               "-out", str(out_file), "-force"]
        if depsdev_enabled:
            cmd += ["-gcp-project-id", gcp_project]
        else:
            cmd += ["-depsdev-disable"]
        cmd.append(str(in_file))

        env = {**os.environ, "GITHUB_AUTH_TOKEN": token_str, "GITHUB_TOKEN": token_str}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=SUBPROCESS_TIMEOUT_S, env=env)
        except subprocess.TimeoutExpired:
            log.warning("criticality_score chunk timed out after %ds", SUBPROCESS_TIMEOUT_S)
            proc = None

        results: dict[str, dict] = {}
        if proc is not None and out_file.exists():
            with open(out_file, encoding="utf-8") as f:
                for tool_row in csv.DictReader(f):
                    repo = slug_from_url(tool_row.get("repo.url", ""))
                    if not repo:
                        continue
                    results[repo] = _to_row(repo, tool_row, repo_ids, depsdev_enabled, now_iso)
        if proc is not None and proc.returncode != 0:
            log.warning("criticality_score chunk exit %d: %s",
                        proc.returncode, (proc.stderr or "")[-300:])

    # Classify the failure once for the chunk (accurate at the default chunk=1).
    timed_out = proc is None
    reason, retryable = _classify_failure("" if timed_out else (proc.stderr or ""), timed_out)

    # Mark any requested repo the tool didn't return as an error row.
    for r in repos:
        rl = r.lower()
        if rl not in results:
            results[rl] = {
                **{k: "" for k in FIELDS},
                "repo": rl, "repo_id": repo_ids.get(rl, ""),
                "depsdev_enabled": "True" if depsdev_enabled else "False",
                "checked_at": now_iso, "status": "error",
                "error_reason": reason, "_retryable": retryable,
            }
    return results


def fetch_with_retry(repos: list[str], token_str: str, workers: int,
                     gcp_project: str | None, repo_ids: dict[str, str],
                     retries: int) -> dict[str, dict]:
    """run_chunk, then re-run any repos that errored (transient secondary limits).

    Backs off progressively (BACKOFF_BASE_S × attempt) because GitHub secondary
    rate limits ask callers to "wait a few minutes". A repo still failing after
    `retries` stays status='error' and is retried on the next pipeline run (TTL).
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    results = run_chunk(repos, token_str, workers, gcp_project, repo_ids, now)
    for attempt in range(1, retries + 1):
        # Only retry TRANSIENT failures (rate limits); deterministic ones
        # (unresolvable-contributor, timeout) stay error without a backoff-spin.
        failed = [r for r in repos
                  if results.get(r.lower(), {}).get("status") != "ok"
                  and results.get(r.lower(), {}).get("_retryable")]
        if not failed:
            break
        backoff = BACKOFF_BASE_S * attempt
        log.info("retry %d/%d for %d repo(s) after %ds backoff", attempt, retries,
                 len(failed), backoff)
        time.sleep(backoff)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        results.update(run_chunk(failed, token_str, workers, gcp_project, repo_ids, now))
    return results


def _to_row(repo: str, tool_row: dict, repo_ids: dict[str, str],
            depsdev_enabled: bool, now_iso: str) -> dict:
    """Map one criticality_score CSV row to our schema."""
    row = {k: "" for k in FIELDS}
    row["repo"] = repo
    row["repo_id"] = repo_ids.get(repo, "")
    for tool_col, our_col in COLMAP.items():
        row[our_col] = (tool_row.get(tool_col) or "").strip()
    row["depsdev_enabled"] = "True" if depsdev_enabled else "False"
    row["checked_at"] = now_iso
    # A real measurement has a numeric score; otherwise flag it.
    try:
        float(row["criticality_score"])
        row["status"] = "ok"
        row["error_reason"] = ""
    except (TypeError, ValueError):
        row["status"] = "error"
        row["error_reason"] = "no-score"
    return row


def write_csv(path: Path, rows_by_repo: dict[str, dict]) -> None:
    """Atomically write the wide CSV, sorted by criticality_score desc."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def _score(r: dict) -> float:
        try:
            return float(r.get("criticality_score") or -1)
        except ValueError:
            return -1.0

    ordered = sorted(rows_by_repo.values(), key=lambda r: (-_score(r), r.get("repo", "")))
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)
    tmp.replace(path)


def _print_coverage(rows_by_repo: dict[str, dict], scope: list[str]) -> None:
    scoped = [rows_by_repo.get(r.lower(), {}) for r in scope]
    ok = sum(1 for r in scoped if (r.get("status") or "") == "ok")
    err = sum(1 for r in scoped if (r.get("status") or "") == "error")
    missing = sum(1 for r in scoped if not r)
    total = len(scope)
    table = Table(title="[bold]Criticality Score coverage (top repos)[/bold]",
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Status", style="bold")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")
    for label, n in (("ok", ok), ("error", err), ("not yet fetched", missing)):
        table.add_row(label, f"{n:,}", f"{100*n/total:.1f}%" if total else "—")
    table.add_row("[bold]total scope[/bold]", f"[bold]{total:,}[/bold]", "[bold]100%[/bold]")
    console.print(table)
    if err:
        reasons: dict[str, int] = {}
        for r in scoped:
            if (r.get("status") or "") == "error":
                k = r.get("error_reason") or "error"
                reasons[k] = reasons.get(k, 0) + 1
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        console.print(f"[dim]error reasons — {breakdown}[/dim]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("repos", nargs="*", help="owner/repo slugs (default: all top repos)")
    p.add_argument("--force", action="store_true", help="ignore the 1-year TTL and refetch")
    # The pipeline runs this as a net=True step, which appends --offline/--refresh.
    p.add_argument("--refresh", action="store_true",
                   help="ignore the TTL and refetch (alias of --force; the pipeline's flag)")
    p.add_argument("--offline", action="store_true",
                   help="never fetch — use only the cached criticality.csv")
    p.add_argument("--workers", type=int, default=WORKERS_DEFAULT, help="binary -workers concurrency")
    p.add_argument("--chunk", type=int, default=CHUNK_DEFAULT,
                   help="repos per binary invocation (>1 risks losing repos to batch-abort)")
    p.add_argument("--throttle", type=float, default=THROTTLE_DEFAULT,
                   help="seconds to pause between repos (keeps search/commits under its limit)")
    p.add_argument("--retries", type=int, default=RETRIES_DEFAULT,
                   help="per-repo retries on a rate-limit failure")
    p.add_argument("--gcp-project", default=None,
                   help="enable deps.dev dependents via this GCP project (needs ADC); "
                        "omitted = -depsdev-disable (github_mention_count proxy)")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()

    # Archived repos are IN scope (load_top_slugs includes them by default):
    # value.csv's `criticality` column must be non-empty for every valid
    # class-A GitHub repo, archived included — the tool scores them fine.
    scope = [r.lower()
             for r in (args.repos or load_top_slugs())]
    scope = list(dict.fromkeys(scope))  # dedupe, preserve order
    if not scope:
        raise SystemExit("No repos — pass slugs or populate data/value/value.csv")

    existing = load_existing(OUTPUT_FILE)
    repo_ids = _repo_id_map()
    healed = _heal_rows(existing, repo_ids, set(scope))
    if healed:
        write_csv(OUTPUT_FILE, existing)
        console.print(f"[yellow]Healed {healed} stale row(s) "
                      f"(repo_id backfill / rename dedupe) → rewrote file[/yellow]")

    if args.offline:
        todo = []
    elif args.force or args.refresh:
        todo = list(scope)
    else:
        todo = [r for r in scope if not _is_fresh(existing.get(r, {}), TTL_DAYS)]

    fresh = len(scope) - len(todo)
    console.print(
        f"[bold]OpenSSF Criticality Score[/bold] — scope {len(scope):,} top repos · "
        f"{fresh:,} fresh (TTL {TTL_DAYS}d, skipped) · {len(todo):,} to fetch · "
        f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}"
        + (" · [yellow]offline[/yellow]" if args.offline else "")
    )
    if not todo:
        console.print("[green]All top repos fresh — nothing to fetch.[/green]")
        _print_coverage(existing, scope)
        return

    token_str = ",".join(_github_tokens())
    rows = dict(existing)

    done = 0
    for i in range(0, len(todo), args.chunk):
        chunk = todo[i:i + args.chunk]
        chunk_rows = fetch_with_retry(chunk, token_str, args.workers, args.gcp_project,
                                      repo_ids, args.retries)
        rows.update(chunk_rows)
        write_csv(OUTPUT_FILE, rows)  # persist after every batch (resumable)
        done += len(chunk)
        if done % 25 == 0 or done == len(todo) or args.chunk > 1:
            ok_total = sum(1 for r in rows.values() if r.get("status") == "ok")
            console.print(f"[dim]{done:,}/{len(todo):,} fetched · {ok_total:,} ok total · "
                          f"last: {chunk[-1]}[/dim]")
        if done < len(todo) and args.throttle:
            time.sleep(args.throttle)

    console.print(f"\n[green]Wrote {len(rows):,} rows → {OUTPUT_FILE}[/green]")
    _print_coverage(rows, scope)


if __name__ == "__main__":
    main()
