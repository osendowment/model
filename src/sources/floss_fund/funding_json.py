"""Download and extract FLOSS Fund funding manifests to CSV.

Source: https://dir.floss.fund/funding-manifests.tar.gz

Re-running within the shared funding TTL (365 days) is a no-op: the output file
is considered fresh, so nothing is downloaded and nothing is rewritten.

Usage:
    python -m src.sources.floss_fund.funding_json
    python -m src.sources.floss_fund.funding_json --ttl 0   # force refresh
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime
import io
import json
import logging
import os
import tarfile
import time

import requests
from rich.console import Console
from rich.table import Table

from src.common.freshness import FUNDING_TTL_DAYS, file_is_fresh
from src.common.funding_platforms import (
    FUNDING_PLATFORMS,
    platform_handle_from_url,
)
from src.common.repos import canonical_repo_map, load_repo_ids, load_value_repo_ids
from src.sources.floss_fund.directory import export_repo_slug, normalize_github_repo

log = logging.getLogger(__name__)
console = Console()

SOURCE_URL = "https://dir.floss.fund/funding-manifests.tar.gz"
OUTPUT_FILE = "data/sources/floss-fund/funding-json.csv"
RESOLVED_FIELD = "project_repository_resolved"

OUTPUT_FIELDS = [
    "id", "url", "status",
    "entity_name", "entity_type", "entity_email", "entity_webpage",
    "project_name", "project_guid", "project_description",
    "project_licenses", "project_tags",
    "project_webpage", "project_repository", RESOLVED_FIELD,
    "funding_channels", "channel_platforms", "funding_plans_count",
] + FUNDING_PLATFORMS + [
    "created_at", "updated_at",
    # repo_id: stable GitHub numeric id for the manifest's repo (rename-proof
    # join key); blank when the manifest is org-level / non-GitHub / unknown to
    # the model's repo maps. fetched_at: UTC ISO-8601 stamp of the run that
    # fetched/refreshed the row (created_at/updated_at above are manifest
    # metadata from the directory, not our fetch date).
    "repo_id", "fetched_at",
]


def parse_channels(channels: list) -> tuple[dict[str, str], str]:
    """Map a manifest's funding.channels to (per-platform handles, channel_platforms).

    Each channel's `address` URL is resolved to a canonical platform + handle
    (see `platform_handle_from_url`); an address with no recognised host lands
    under `custom`. `channel_platforms` is the comma-joined distinct platform
    names, falling back to the channel `type` (e.g. `bank`) when a channel has
    no usable address — the union key the funding builder counts.
    """
    handles: dict[str, list[str]] = {}
    seen_platforms: list[str] = []

    def _note(name: str) -> None:
        if name and name not in seen_platforms:
            seen_platforms.append(name)

    for ch in channels or []:
        if not isinstance(ch, dict):
            continue
        address = (ch.get("address") or "").strip()
        platform, handle = platform_handle_from_url(address) if address else (None, "")
        if platform:
            handles.setdefault(platform, []).append(handle)
            _note(platform)
        elif address:
            handles.setdefault("custom", []).append(handle)
            _note("custom")
        else:
            _note((ch.get("type") or "").strip())  # e.g. bank — no address
    cols = {p: ",".join(handles[p]) for p in handles}
    return cols, ",".join(seen_platforms)


def _redirect_candidate_urls(rows: list[dict],
                             known_github_slugs: set[str] | None = None) -> set[str]:
    """Distinct `project_repository` URLs worth a redirect probe.

    A non-GitHub http(s) URL that could redirect to a GitHub repo (e.g.
    `tukaani.org/xz/redirect-to-github-xz`), PLUS a GitHub URL whose owner/repo
    the pipeline cannot already resolve (`known_github_slugs`) — a slug the model
    knows only under the repo's NEW name won't match a manifest still declaring
    the OLD one, so following GitHub's rename redirect recovers it. When
    `known_github_slugs` is None, GitHub URLs are all treated as known (legacy
    behaviour: skip them). Non-URL values are always skipped.
    """
    out: set[str] = set()
    for r in rows:
        u = (r.get("project_repository") or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        gh = normalize_github_repo(u)
        if gh is None:
            out.add(u)
        elif known_github_slugs is not None and gh.lower() not in known_github_slugs:
            out.add(u)
    return out


def _resolve_url(url: str, timeout: int = 8) -> str | None:
    """Follow redirects; return the final URL iff it lands on a GitHub repo.

    Tries HEAD first (cheap), then GET (some hosts only redirect on GET). Any
    network/TLS error → None (best-effort; the row keeps its raw URL).
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; OSE-model funding resolver)"}
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        final = resp.url
        if normalize_github_repo(final) is None:
            resp = requests.get(url, allow_redirects=True, timeout=timeout,
                                headers=headers, stream=True)
            final = resp.url
            resp.close()
    except requests.RequestException as e:
        log.debug("redirect resolve failed for %s: %s", url, e)
        return None
    return final if normalize_github_repo(final) else None


def _load_last_good_resolved(filepath: str) -> dict[str, str]:
    """Map {project_repository(raw) -> project_repository_resolved} from prior output.

    Only rows that already carry a non-empty resolved value are kept — this is the
    "last-good" cache that protects a previously-resolved URL from being wiped by a
    transient network failure on the next run. A missing/unreadable file → empty map.
    """
    last_good: dict[str, str] = {}
    if not os.path.exists(filepath):
        return last_good
    try:
        with open(filepath, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                raw = (row.get("project_repository") or "").strip()
                resolved = (row.get(RESOLVED_FIELD) or "").strip()
                if raw and resolved:
                    last_good[raw] = resolved
    except OSError as e:
        log.debug("could not load last-good resolved map from %s: %s", filepath, e)
    return last_good


def resolve_repo_redirects(rows: list[dict], resolver=_resolve_url,
                           max_workers: int = 16,
                           last_good: dict[str, str] | None = None,
                           known_github_slugs: set[str] | None = None) -> int:
    """Populate `project_repository_resolved` on every row; return count resolved.

    Probes the redirect candidates (`_redirect_candidate_urls`), in parallel, and
    records the final GitHub URL when one redirects there. That set is every
    non-GitHub repo URL PLUS any GitHub URL whose owner/repo the pipeline can't
    already resolve — `known_github_slugs`, defaulting to the union of the
    canonical-repo-map keys (which already include both the old `repo` slug and
    the new `full_name`) and value.csv's slugs (fresh renames repos.csv hasn't
    caught up on). A manifest declaring a repo's OLD slug that the model knows
    only under its NEW name thus gets its rename redirect followed instead of
    landing with a blank repo_id and being dropped. Rows that already resolve (or
    don't redirect to GitHub) get an empty resolved value — consumers fall back
    to the raw URL via `export_repo_slug`.

    `last_good` (raw URL → previously-resolved value) makes resolution
    deterministic across runs: a URL that resolved on an earlier run is NEVER
    blanked by a transient network failure this run — we fall back to the
    last-good value instead. Final precedence per row: fresh resolve, else
    last-good, else "".
    """
    last_good = last_good or {}
    if known_github_slugs is None:
        known_github_slugs = set(canonical_repo_map()) | set(load_value_repo_ids())
    candidates = sorted(_redirect_candidate_urls(rows, known_github_slugs))
    mapping: dict[str, str] = {}
    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for url, resolved in zip(candidates, ex.map(resolver, candidates)):
                if resolved:
                    mapping[url] = resolved
    for r in rows:
        raw = (r.get("project_repository") or "").strip()
        r[RESOLVED_FIELD] = mapping.get(raw) or last_good.get(raw, "")
    return len(mapping)


def _disk_repo_ids(path) -> dict[str, str]:
    """{manifest repo slug -> repo_id} already recorded in the output file.

    Base layer of the id map so a directory refresh (whole-file rewrite)
    never wipes an id resolved earlier — in particular the out-of-scope ids
    resolved once via the GitHub API, which the local maps don't know.
    """
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            slug = export_repo_slug(row)
            if rid and slug:
                out.setdefault(slug, rid)
    return out


def resolve_repo_ids(rows: list[dict], ids: dict[str, str] | None = None,
                     canon: dict[str, str] | None = None) -> int:
    """Populate `repo_id` on every row; return the count resolved.

    The manifest's repo slug (`export_repo_slug`: resolved-redirect-or-raw URL,
    lowercased) is passed through `canonical_repo_map` (a manifest URL can
    predate a GitHub rename) and looked up in the model's slug → id maps:
    ids already on disk in the output file (base layer — preserves ids
    resolved once via the GitHub API for out-of-scope repos, unknown to the
    local maps), then `load_repo_ids` (github/repos.csv) overlaid with
    `load_value_repo_ids` (value.csv wins — repos.csv lags renames).
    Org-level manifests, non-GitHub hosts, and unresolvable slugs get an
    empty repo_id — the id is never invented.
    """
    if ids is None:
        ids = {**_disk_repo_ids(OUTPUT_FILE),
               **load_repo_ids(), **load_value_repo_ids()}
    if canon is None:
        canon = canonical_repo_map()
    resolved = 0
    for r in rows:
        slug = export_repo_slug(r)
        rid = ids.get(canon.get(slug, slug), "") if slug else ""
        r["repo_id"] = rid
        if rid:
            resolved += 1
    return resolved


def _download_and_extract(url: str) -> str:
    """Download tar.gz and return the CSV content as string."""
    log.debug("Downloading %s", url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        member = tar.getmember("funding-manifests.csv")
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError("Could not extract funding-manifests.csv from archive")
        return f.read().decode("utf-8")


def _parse_manifests(raw_csv: str) -> list[dict]:
    """Parse raw CSV with embedded JSON into flat rows (one per project)."""
    reader = csv.DictReader(io.StringIO(raw_csv))
    rows: list[dict] = []

    for raw in reader:
        manifest_str = raw.get("manifest_json", "")
        if not manifest_str:
            continue

        try:
            manifest = json.loads(manifest_str)
        except json.JSONDecodeError:
            log.debug("Skipping id=%s: invalid JSON", raw.get("id"))
            continue

        entity = manifest.get("entity", {})
        funding = manifest.get("funding", {})
        projects = manifest.get("projects", [])

        platform_handles, channel_platforms = parse_channels(funding.get("channels") or [])

        base = {
            "id": raw.get("id", ""),
            "url": raw.get("url", ""),
            "status": raw.get("status", ""),
            "entity_name": entity.get("name", ""),
            "entity_type": entity.get("type", ""),
            "entity_email": entity.get("email", ""),
            "entity_webpage": (entity.get("webpageUrl") or {}).get("url", ""),
            "funding_channels": ",".join(
                c.get("type", "") for c in (funding.get("channels") or [])
            ),
            "channel_platforms": channel_platforms,
            "funding_plans_count": len(funding.get("plans") or []),
            **{p: platform_handles.get(p, "") for p in FUNDING_PLATFORMS},
            "created_at": raw.get("created_at", ""),
            "updated_at": raw.get("updated_at", ""),
        }

        if not projects:
            rows.append({**base, "project_name": "", "project_guid": "",
                         "project_description": "", "project_licenses": "",
                         "project_tags": "", "project_webpage": "",
                         "project_repository": ""})
            continue

        for proj in projects:
            rows.append({
                **base,
                "project_name": proj.get("name", ""),
                "project_guid": proj.get("guid", ""),
                "project_description": proj.get("description", ""),
                "project_licenses": ",".join(proj.get("licenses") or []),
                "project_tags": ",".join(proj.get("tags") or []),
                "project_webpage": (proj.get("webpageUrl") or {}).get("url", ""),
                "project_repository": (proj.get("repositoryUrl") or {}).get("url", ""),
            })

    return rows


def _write_csv(filepath: str, rows: list[dict]) -> None:
    """Write parsed rows to CSV, sorted by a stable key for deterministic output."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    rows = sorted(rows, key=lambda r: (str(r.get("id", "")), str(r.get("project_name", ""))))
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download FLOSS Fund funding manifests")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--ttl", type=int, default=FUNDING_TTL_DAYS,
                        help=f"Skip if fetched within N days (default: {FUNDING_TTL_DAYS}, 0 to force)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    fresh = file_is_fresh(args.output, args.ttl)

    if fresh:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(args.output))
        console.print(f"[bold]FLOSS Fund Manifests[/bold]  [dim]cached {mtime:%Y-%m-%d}[/dim]")
        console.print()
        with open(args.output, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        elapsed = 0.0
    else:
        t_start = time.monotonic()
        console.print(f"[bold]FLOSS Fund Manifests[/bold]  [dim]{SOURCE_URL}[/dim]")
        console.print()
        raw_csv = _download_and_extract(SOURCE_URL)
        rows = _parse_manifests(raw_csv)
        console.print("[dim]Resolving non-GitHub repo URLs via redirect…[/dim]")
        last_good = _load_last_good_resolved(args.output)
        resolve_repo_redirects(rows, last_good=last_good)
        # Every row in this branch was genuinely (re-)fetched from the tarball
        # just now — the file is rewritten whole, so one stamp covers the run.
        resolve_repo_ids(rows)
        fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            r["fetched_at"] = fetched_at
        _write_csv(args.output, rows)
        elapsed = time.monotonic() - t_start

    entities = len({r["id"] for r in rows})
    projects = len(rows)
    with_repo = sum(1 for r in rows if r["project_repository"])
    resolved = sum(1 for r in rows if (r.get(RESOLVED_FIELD) or "").strip())
    with_repo_id = sum(1 for r in rows if (r.get("repo_id") or "").strip())
    active = sum(1 for r in rows if r["status"] == "active")

    # Entity type breakdown
    from collections import Counter
    type_counts = Counter(r["entity_type"] for r in rows)
    license_counts: Counter[str] = Counter()
    for r in rows:
        for lic in r["project_licenses"].split(","):
            lic = lic.strip().removeprefix("spdx:")
            if lic:
                license_counts[lic] += 1

    # Top entities by project count
    entity_projects: Counter[str] = Counter()
    for r in rows:
        entity_projects[r["entity_name"]] += 1

    # Results table
    tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Entity")
    tbl.add_column("Type", style="dim")
    tbl.add_column("Projects", justify="right")

    for name, count in entity_projects.most_common(15):
        etype = next((r["entity_type"] for r in rows if r["entity_name"] == name), "")
        tbl.add_row(name, etype, str(count))

    if len(entity_projects) > 15:
        rest = sum(c for _, c in entity_projects.most_common()[15:])
        tbl.add_row(f"[dim]... {len(entity_projects) - 15} more[/dim]", "", f"[dim]{rest}[/dim]")

    tbl.add_section()
    tbl.add_row("[bold]Total[/bold]", "", f"[bold]{projects}[/bold]")

    console.print(tbl)
    console.print()

    # License breakdown
    lic_tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    lic_tbl.add_column("License")
    lic_tbl.add_column("Projects", justify="right")

    for lic, count in license_counts.most_common(10):
        lic_tbl.add_row(lic, str(count))
    if len(license_counts) > 10:
        rest = sum(c for _, c in license_counts.most_common()[10:])
        lic_tbl.add_row(f"[dim]... {len(license_counts) - 10} more[/dim]", f"[dim]{rest}[/dim]")

    console.print(lic_tbl)
    console.print()

    # Summary
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("Entities", f"{entities:,}")
    summary.add_row("Projects", f"[bold]{projects:,}[/bold]")
    summary.add_row("With repo URL", f"{with_repo:,}")
    summary.add_row("Repo URL resolved (redirect→GitHub)", f"{resolved:,}")
    summary.add_row("GitHub repo_id resolved", f"{with_repo_id:,}")
    summary.add_row("Active", f"{active:,}")
    type_str = "  ".join(f"{t}: {c}" for t, c in type_counts.most_common())
    summary.add_row("Types", f"[dim]{type_str}[/dim]")
    if not fresh:
        summary.add_row("Time", f"{elapsed:.1f}s")
        summary.add_row("Output", args.output)
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
