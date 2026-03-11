"""Rich terminal display for contributor metrics."""
from __future__ import annotations

import datetime
from contextlib import contextmanager

from rich.console import Console
from rich.markup import escape as rich_escape
from rich.progress import (
    Progress, ProgressColumn, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn, TimeElapsedColumn,
    Task as RichTask,
)
from rich.table import Table
from rich.text import Text

from src.github.models import Contributor, RunResult, PerfStats, DateRange, THRESHOLD

# Re-usable table style
TABLE_KWARGS = dict(show_header=True, header_style="bold dim", padding=(0, 1))

console = Console()


class _ETAColumn(ProgressColumn):
    """Shows ETA as absolute wall-clock time (e.g. 'ETA 14:32:05')."""

    def render(self, task: RichTask) -> Text:
        remaining = task.time_remaining
        if remaining is None or remaining == float("inf"):
            return Text("", style="dim")
        eta = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
        return Text(f"ETA {eta:%H:%M:%S}", style="dim")


@contextmanager
def _spinner(message: str):
    """Context manager for a transient spinner progress bar."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(message, total=None)
        yield progress


def _fmt_date(d: datetime.date | None, default: str = "–") -> str:
    """Format a date for display."""
    return d.isoformat() if d else default


def _fmt_contribs_label(contributors: list[Contributor]) -> str:
    """Format contributor count as 'N' or 'B + N' with dimmed bot count."""
    humans = [c for c in contributors if not c.is_bot]
    bots = [c for c in contributors if c.is_bot]
    label = ""
    if bots:
        label += f"[bright_black]{len(bots)} + [/bright_black]"
    label += f"{len(humans):,}"
    return label


def display_yearly_breakdown(
    repo: str, year_results: list[tuple[str, RunResult]],
    base: str = "commits",
) -> None:
    """Show a table with per-year rows for key metrics."""
    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Year", style="dim")
    table.add_column("BF", justify="right")
    table.add_column("HHI", justify="right")
    table.add_column("Contribs", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("LOC", justify="right")
    table.add_column("First", justify="right")
    table.add_column("Last", justify="right")

    total_label = year_results[-1][0]
    year_labels = [label for label, _ in year_results[:-1]]
    range_str = f"{year_labels[0]}–{year_labels[-1]}" if len(year_labels) > 1 else year_labels[0] if year_labels else total_label

    for label, r in year_results:
        humans = [c for c in r.contributors if not c.is_bot]
        total_commits = sum(c.commits for c in humans)
        is_total = label == total_label

        loc_str = f"{r.total_loc:,}" if r.total_loc is not None and r.total_loc > 0 else "[dim]–[/dim]"

        table.add_row(
            "[bold]Total[/bold]" if is_total else label,
            f"[yellow bold]{r.bus_factor}[/yellow bold]" if r.bus_factor > 0 else "[dim]–[/dim]",
            f"{round(r.hhi * 10000):,}" if r.hhi > 0 else "[dim]–[/dim]",
            _fmt_contribs_label(r.contributors) if humans else "[dim]–[/dim]",
            f"{total_commits:,}" if total_commits else "[dim]–[/dim]",
            loc_str,
            _fmt_date(r.first_week),
            _fmt_date(r.last_week),
            end_section=(label == year_labels[-1]),
        )

    console.print()
    console.print(f"[bold]Contributor Analysis for [cyan]{repo}[/cyan] ({range_str}) by {base}[/bold]")
    console.print()
    console.print(table)


def display_results(
    repo: str, result: RunResult, date_range: DateRange | None = None,
    base: str = "commits", skip_summary: bool = False,
) -> None:
    """Print a rich table of results."""
    bus_factor = result.bus_factor
    contributors = result.contributors
    hhi = result.hhi
    perf = result.perf

    if not skip_summary:
        console.print()
        dr = date_range or DateRange()
        range_label = f" ([magenta]{dr.label}[/magenta])" if dr.label else ""
        base_label = f" [dim]by {base}[/dim]"
        console.print(f"[bold]Bus Factor for [cyan]{repo}[/cyan]{range_label}{base_label}: [yellow]{bus_factor}[/yellow][/bold]")
        console.print()

        human_contribs = [c for c in contributors if not c.is_bot]
        total_commits = sum(c.commits for c in human_contribs)
        summary = Table(show_header=False, padding=(0, 1), box=None)
        summary.add_column(style="dim")
        summary.add_column()
        summary.add_row("Bus Factor", f"[yellow bold]{bus_factor}[/yellow bold]")
        summary.add_row("HHI", f"{round(hhi * 10000):,}")
        summary.add_row("Contributors", _fmt_contribs_label(contributors))
        summary.add_row("Commits", f"{total_commits:,}")
        if result.first_week and result.last_week:
            summary.add_row("First Date", str(result.first_week))
            summary.add_row("Last Date", str(result.last_week))
        if base == "locs":
            total_changed = sum(c.lines_changed for c in contributors)
            total_loc = sum(c.loc for c in contributors)
            summary.add_row("Lines Changed", f"{total_changed:,}")
            summary.add_row("Net LOC", f"{total_loc:+,}")
        console.print(summary)
    console.print()

    has_locs = any(c.lines_changed > 0 for c in contributors)
    loc_header = "[bold underline]LOC[/bold underline]" if base == "locs" else "LOC"
    changed_header = "[bold underline]Changed[/bold underline]" if base == "locs" else "Changed"
    commits_header = "[bold underline]Commits[/bold underline]" if base == "commits" else "Commits"

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Contributor", min_width=16)
    table.add_column(loc_header, justify="right")
    table.add_column(changed_header, justify="right")
    table.add_column(commits_header, justify="right")
    table.add_column("%", justify="right")
    table.add_column("Cumul%", justify="right")

    cumulative = 0.0
    threshold_crossed = False
    show_count = min(20, len(contributors))
    human_idx = 0

    for c in contributors[:show_count]:
        if not c.is_bot:
            cumulative += c.pct
            human_idx += 1

        display_name = rich_escape(c.login)
        if c.is_bot:
            display_name = f"[bright_black]{display_name}[/bright_black]"
            num_str = ""
        else:
            num_str = str(human_idx)

        loc_str = f"{c.loc:+,}" if has_locs and (c.lines_added or c.lines_deleted) else ""
        changed_str = f"{c.lines_changed:,}" if has_locs and c.lines_changed else ""
        if c.is_bot:
            row = [num_str, display_name,
                   f"[bright_black]{loc_str}[/bright_black]",
                   f"[bright_black]{changed_str}[/bright_black]",
                   f"[bright_black]{c.commits:,}[/bright_black]", "", ""]
            table.add_row(*row)
        else:
            row = [num_str, display_name, loc_str, changed_str,
                   f"{c.commits:,}", f"{c.pct:.1%}", f"{cumulative:.1%}"]
            table.add_row(*row, style="yellow" if not threshold_crossed else "")

        if not c.is_bot and not threshold_crossed and cumulative >= THRESHOLD:
            threshold_crossed = True

    if len(contributors) > show_count:
        rest = contributors[show_count:]
        rest_humans = [c for c in rest if not c.is_bot]
        rest_commits = sum(c.commits for c in rest_humans)
        rest_pct = sum(c.pct for c in rest_humans)
        rest_loc = sum(c.loc for c in rest_humans)
        rest_lines = sum(c.lines_changed for c in rest_humans)
        has_rest_loc = has_locs and any(c.lines_added or c.lines_deleted for c in rest_humans)
        rest_bots = len(rest) - len(rest_humans)
        more_label = f"{len(rest_humans)} more"
        if rest_bots:
            more_label += f" + {rest_bots} bots"
        row = ["", f"[dim]... {more_label}[/dim]",
               f"[dim]{rest_loc:+,}[/dim]" if has_rest_loc else "",
               f"[dim]{rest_lines:,}[/dim]" if has_locs and rest_lines else "",
               f"[dim]{rest_commits:,}[/dim]", f"[dim]{rest_pct:.1%}[/dim]", "[dim]100.0%[/dim]"]
        table.add_row(*row)

    console.print(table)

    if perf:
        console.print()
        parts = [f"[dim]{perf.total:.1f}s total[/dim]"]
        if perf.source == "git":
            parts.append(f"[dim]clone {perf.git_clone:.1f}s[/dim]")
            parts.append(f"[dim]parse {perf.git_parse:.1f}s[/dim]")
        parts.append(f"[dim]api {perf.api_fetch:.1f}s[/dim]")
        console.print(" · ".join(parts))
    console.print()


# --- Search display ---

def fmt_step_summary(label: str, count: int, elapsed: float) -> str:
    """Format a completed step as a one-line summary."""
    per = elapsed / count * 1000 if count else 0
    return f"  [dim]{label:<16} {count:>5,}  {elapsed:>5.1f}s  {per:>5.0f}ms/ea[/]"


class _StatusColumn(ProgressColumn):
    """Shows a dynamic status message from task.fields['status_fn'], if set."""

    def render(self, task: RichTask) -> Text:
        fn = task.fields.get("status_fn")
        if fn:
            msg = fn()
            if msg:
                return Text(msg, style="yellow")
        return Text("")


def make_search_progress(status_fn: "Callable[[], str] | None" = None) -> Progress:
    """Create a Progress bar matching the contributor metrics style.

    status_fn: optional callable returning a status string (e.g. rate limit info).
    Stored in task fields so it's available to _StatusColumn.
    """
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=14),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        _ETAColumn(),
        _StatusColumn(),
        console=console,
    )
    progress._status_fn = status_fn  # store for callers to pass to add_task
    return progress


def display_search_summary(
    repo_count: int,
    elapsed: float,
    api_calls: int,
    output: str | None,
    repos_path: str,
    total: int,
    added: int,
    updated: int,
    top_repo: str | None = None,
    top_stars: int = 0,
) -> None:
    """Show final search summary in a table."""
    console.print()

    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim")
    summary.add_column()

    summary.add_row("Repos found", f"[bold]{repo_count:,}[/bold]")
    summary.add_row("Time", f"{elapsed:.1f}s ({api_calls} API calls)")
    if output:
        summary.add_row("Output", output)
    summary.add_row(
        "top-repos.csv",
        f"{repos_path} ({total:,} total, "
        f"[green]{added:,} added[/], [yellow]{updated:,} updated[/])",
    )
    if top_repo:
        summary.add_row("Top", f"{top_repo} ({top_stars:,} stars)")

    console.print(summary)
