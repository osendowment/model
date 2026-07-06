"""Shared orchestration helper for pipeline runners.

A pipeline is an ordered list of `Step`s. `run_pipeline` runs each as
`python -m <module>` in a subprocess, honouring --from / --only / --list.
Steps flagged `net=True` require network access — they receive --offline /
--refresh when those flags are set, so TTL caches make fresh data zero-network.
Steps flagged `pipeline=True` are sub-orchestrators; they also receive the
--offline / --refresh flags so they can propagate them to their own net steps.
Consecutive steps sharing a `pgroup` run CONCURRENTLY (the group boundary is a
barrier; use for independent fetchers only).

Presentation: every step (serial or grouped) runs under one live progress
display — a step's spinner + elapsed row becomes visible once it has been
running for `SHOW_PROGRESS_AFTER_S` seconds, so quick steps don't flash noise.
Each step's own output (the detailed stats the fetchers/builders print) is
captured and echoed in full when a long step finishes, and the run ends with a
per-step timing summary table, slowest first.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()

# A step's live progress row appears only after it has run this long, and only
# such long steps get their full output echoed ("progress bars + detailed
# stats for steps taking 5+ seconds"); quick steps print a one-line ✓.
SHOW_PROGRESS_AFTER_S = 5.0


@dataclass
class Step:
    label: str              # short name, used by --from / --only
    module: str             # dotted module path, run via `python -m`
    fetch: bool = False     # legacy field — kept for backward compat; unused by select_steps
    pipeline: bool = False  # step is itself an orchestrator — receives --offline/--refresh
    net: bool = False       # step does network I/O — receives --offline/--refresh
    pgroup: str | None = None  # consecutive steps sharing a pgroup run CONCURRENTLY
    # (a pgroup boundary is a barrier: the next step/group starts only after
    # every step in the group succeeded — use for independent fetchers only)


def select_steps(steps: list[Step], from_step: str | None,
                 only: str | None, offline: bool = False) -> list[Step]:
    """Resolve --from / --only into the list of steps to run.

    All steps run by default (TTL makes cached data zero-network). Use
    --refresh to force refetch. `offline` (from --offline) hard-forbids
    network: `net=True` steps are kept and receive --offline so they use only
    their caches, but legacy fetch-only steps (`fetch=True and not net=True` —
    they do network yet can't honour --offline) are DROPPED, so a shared
    consumer like the risk/eligibility pipeline can still run builders-only
    offline (the role the old --skip-fetch served). Raises KeyError if
    `from_step` / `only` names an unknown step.
    """
    labels = [s.label for s in steps]
    if only is not None:
        if only not in labels:
            raise KeyError(only)
        chosen = [s for s in steps if s.label == only]
    elif from_step is not None:
        if from_step not in labels:
            raise KeyError(from_step)
        chosen = steps[labels.index(from_step):]
    else:
        chosen = list(steps)
    if offline:
        chosen = [s for s in chosen if not (s.fetch and not s.net)]
    return chosen


def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--from", dest="from_step", metavar="STEP",
                   help="Start from this step, skipping earlier ones")
    p.add_argument("--only", metavar="STEP", help="Run only this step")
    p.add_argument("--offline", action="store_true",
                   help="Hard-forbid network access in net steps (use only cached data)")
    p.add_argument("--refresh", action="store_true",
                   help="Force refetch in net steps, ignoring TTL caches")
    p.add_argument("--list", action="store_true", help="List steps and exit")
    return p


def _step_cmd(s: Step, net_flags: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", s.module]
    if net_flags and (s.net or s.pipeline):
        cmd.extend(net_flags)
    return cmd


def _clean_output(raw: str) -> str:
    """Collapse progress-bar carriage returns and squeeze blank runs so a
    step's captured output stays readable when echoed after completion."""
    lines = [ln.rstrip() for ln in raw.replace("\r", "\n").splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def _run_batch(batch: list[Step], net_flags: list[str],
               timings: list[tuple[str, float, int]]) -> int:
    """Run 1..N steps concurrently under one live progress display.

    A step's spinner + elapsed row appears once it has run for
    SHOW_PROGRESS_AFTER_S; long steps echo their full captured output (their
    own detailed stats) on completion, quick ones print a one-line ✓. All
    steps run to completion even if a sibling fails (no mid-flight kills —
    cleaner on-disk state); the first non-zero exit code is returned after
    the whole batch finishes.
    """
    if len(batch) > 1:
        console.print(f"\n[bold cyan]▶ parallel ×{len(batch)}[/bold cyan] "
                      + ", ".join(s.label for s in batch))
    outputs: dict[str, str] = {}
    threads: dict[str, threading.Thread] = {}
    running: dict[str, tuple[Step, subprocess.Popen, object, float]] = {}
    first_rc = 0

    with Progress(SpinnerColumn(),
                  TextColumn("[bold]{task.description}"),
                  TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        for s in batch:
            proc = subprocess.Popen(_step_cmd(s, net_flags),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            tid = progress.add_task(f"{s.label} [dim]({s.module})[/dim]",
                                    total=None, visible=False)
            t = threading.Thread(target=lambda lbl=s.label, pipe=proc.stdout:
                                 outputs.__setitem__(lbl, pipe.read()), daemon=True)
            t.start()
            threads[s.label] = t
            running[s.label] = (s, proc, tid, time.monotonic())

        while running:
            for label in list(running):
                s, proc, tid, t0 = running[label]
                if time.monotonic() - t0 >= SHOW_PROGRESS_AFTER_S:
                    progress.update(tid, visible=True)
                if proc.poll() is None:
                    continue
                threads[label].join()
                progress.remove_task(tid)
                del running[label]
                dt = time.monotonic() - t0
                timings.append((s.label, dt, proc.returncode))
                out = _clean_output(outputs.get(label, ""))
                header = f"[bold]{s.label}[/bold] [dim]({s.module})[/dim]"
                if proc.returncode != 0:
                    progress.console.print(f"\n[red]✗[/red] {header} "
                                           f"[red]FAILED exit {proc.returncode} "
                                           f"after {dt:.0f}s[/red]")
                    if out:
                        progress.console.print(out)
                    print(f"FAILED: {s.label} (exit {proc.returncode}, {dt:.0f}s)",
                          file=sys.stderr)
                    if first_rc == 0:
                        first_rc = proc.returncode
                elif dt >= SHOW_PROGRESS_AFTER_S:
                    # Long step — echo its full output: the detailed stats.
                    progress.console.print(f"\n[green]✓[/green] {header} — {dt:.1f}s")
                    if out:
                        progress.console.print(out, highlight=False)
                else:
                    tail = out.splitlines()[-1] if out else ""
                    progress.console.print(
                        f"[green]✓[/green] {header} — {dt:.1f}s"
                        + (f"  [dim]{tail[:80]}[/dim]" if tail else ""))
            time.sleep(0.1)
    return first_rc


def _print_summary(timings: list[tuple[str, float, int]]) -> None:
    """Per-step timing summary — slowest steps first, total last."""
    if not timings:
        return
    table = Table(title="Step timing", header_style="bold dim")
    table.add_column("step")
    table.add_column("time", justify="right")
    table.add_column("status", justify="right")
    for label, dt, rc in sorted(timings, key=lambda t: -t[1]):
        status = "[green]ok[/green]" if rc == 0 else f"[red]exit {rc}[/red]"
        table.add_row(label, f"{dt:.1f}s", status)
    table.add_row("[bold]total (sum)[/bold]",
                  f"[bold]{sum(dt for _, dt, _ in timings):.1f}s[/bold]", "")
    console.print()
    console.print(table)


def run_pipeline(steps: list[Step], args: argparse.Namespace) -> int:
    """Execute the selected steps as subprocesses. Returns an exit code."""
    if args.list:
        for s in steps:
            tags = " ".join(t for t, on in
                            (("[fetch]", s.fetch), ("[pipeline]", s.pipeline),
                             ("[net]", s.net),
                             (f"[pgroup={s.pgroup}]", bool(s.pgroup))) if on)
            print(f"  {s.label:24s} {s.module}  {tags}".rstrip())
        return 0
    try:
        selected = select_steps(steps, args.from_step, args.only,
                                offline=getattr(args, "offline", False))
    except KeyError as e:
        print(f"unknown step: {e.args[0]}", file=sys.stderr)
        return 2

    # Build extra flags for net/pipeline steps.
    net_flags: list[str] = []
    if getattr(args, "offline", False):
        net_flags.append("--offline")
    if getattr(args, "refresh", False):
        net_flags.append("--refresh")

    timings: list[tuple[str, float, int]] = []
    rc_final = 0
    i = 0
    while i < len(selected):
        s = selected[i]
        batch = [s]
        if s.pgroup:
            while (i + len(batch) < len(selected)
                   and selected[i + len(batch)].pgroup == s.pgroup):
                batch.append(selected[i + len(batch)])
        rc = _run_batch(batch, net_flags, timings)
        if rc != 0:
            rc_final = rc
            break
        i += len(batch)

    _print_summary(timings)
    return rc_final
