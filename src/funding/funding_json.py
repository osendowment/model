"""Download and extract FLOSS Fund funding manifests to CSV.

Source: https://dir.floss.fund/funding-manifests.tar.gz

Usage:
    python -m src.funding.funding_json
    python -m src.funding.funding_json --ttl 0   # force refresh
"""
from __future__ import annotations

import argparse
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

log = logging.getLogger(__name__)
console = Console()

SOURCE_URL = "https://dir.floss.fund/funding-manifests.tar.gz"
OUTPUT_FILE = "data/floss-fund/funding-json.csv"
DEFAULT_TTL_DAYS = 30

OUTPUT_FIELDS = [
    "id", "url", "status",
    "entity_name", "entity_type", "entity_email", "entity_webpage",
    "project_name", "project_guid", "project_description",
    "project_licenses", "project_tags",
    "project_webpage", "project_repository",
    "funding_channels", "funding_plans_count",
    "created_at", "updated_at",
]


def _is_fresh(filepath: str, ttl_days: int) -> bool:
    """Check if the output file was modified within TTL."""
    if ttl_days <= 0 or not os.path.exists(filepath):
        return False
    mtime = os.path.getmtime(filepath)
    age = time.time() - mtime
    return age < ttl_days * 86400


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
            "funding_plans_count": len(funding.get("plans") or []),
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
    """Write parsed rows to CSV."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download FLOSS Fund funding manifests")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip if fetched within N days (default: {DEFAULT_TTL_DAYS}, 0 to force)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    fresh = _is_fresh(args.output, args.ttl)

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
        _write_csv(args.output, rows)
        elapsed = time.monotonic() - t_start

    entities = len({r["id"] for r in rows})
    projects = len(rows)
    with_repo = sum(1 for r in rows if r["project_repository"])
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
