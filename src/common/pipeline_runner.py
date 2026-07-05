"""Shared orchestration helper for pipeline runners.

A pipeline is an ordered list of `Step`s. `run_pipeline` runs each as
`python -m <module>` in a subprocess, honouring --from / --only / --list.
Steps flagged `net=True` require network access — they receive --offline /
--refresh when those flags are set, so TTL caches make fresh data zero-network.
Steps flagged `pipeline=True` are sub-orchestrators; they also receive the
--offline / --refresh flags so they can propagate them to their own net steps.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field


@dataclass
class Step:
    label: str              # short name, used by --from / --only
    module: str             # dotted module path, run via `python -m`
    fetch: bool = False     # legacy field — kept for backward compat; unused by select_steps
    pipeline: bool = False  # step is itself an orchestrator — receives --offline/--refresh
    net: bool = False       # step does network I/O — receives --offline/--refresh


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


def run_pipeline(steps: list[Step], args: argparse.Namespace) -> int:
    """Execute the selected steps as subprocesses. Returns an exit code."""
    if args.list:
        for s in steps:
            tags = " ".join(t for t, on in
                            (("[fetch]", s.fetch), ("[pipeline]", s.pipeline),
                             ("[net]", s.net)) if on)
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

    for s in selected:
        print(f"\n=== {s.label} ({s.module}) ===", flush=True)
        cmd = [sys.executable, "-m", s.module]
        if net_flags and (s.net or s.pipeline):
            cmd.extend(net_flags)
        t0 = time.monotonic()
        result = subprocess.run(cmd)
        dt = time.monotonic() - t0
        if result.returncode != 0:
            print(f"FAILED: {s.label} (exit {result.returncode}, {dt:.0f}s)",
                  file=sys.stderr)
            return result.returncode
        print(f"--- {s.label} done in {dt:.0f}s", flush=True)
    return 0
