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

    i = 0
    while i < len(selected):
        s = selected[i]
        # Collect a run of consecutive steps sharing the same pgroup.
        batch = [s]
        if s.pgroup:
            while (i + len(batch) < len(selected)
                   and selected[i + len(batch)].pgroup == s.pgroup):
                batch.append(selected[i + len(batch)])
        if len(batch) > 1:
            rc = _run_parallel(batch, net_flags)
            if rc != 0:
                return rc
            i += len(batch)
            continue

        print(f"\n=== {s.label} ({s.module}) ===", flush=True)
        cmd = _step_cmd(s, net_flags)
        t0 = time.monotonic()
        result = subprocess.run(cmd)
        dt = time.monotonic() - t0
        if result.returncode != 0:
            print(f"FAILED: {s.label} (exit {result.returncode}, {dt:.0f}s)",
                  file=sys.stderr)
            return result.returncode
        print(f"--- {s.label} done in {dt:.0f}s", flush=True)
        i += 1
    return 0


def _step_cmd(s: Step, net_flags: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", s.module]
    if net_flags and (s.net or s.pipeline):
        cmd.extend(net_flags)
    return cmd


def _run_parallel(batch: list[Step], net_flags: list[str]) -> int:
    """Run a pgroup batch concurrently; print each step's captured output in
    batch order as it completes. All steps run to completion even if a
    sibling fails (no mid-flight kills — cleaner on-disk state); the first
    non-zero exit code is returned after the whole batch finishes."""
    labels = ", ".join(s.label for s in batch)
    print(f"\n=== [parallel ×{len(batch)}] {labels} ===", flush=True)
    t0 = time.monotonic()
    procs = [subprocess.Popen(_step_cmd(s, net_flags),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
             for s in batch]
    first_rc = 0
    for s, p in zip(batch, procs):
        out, _ = p.communicate()
        dt = time.monotonic() - t0
        print(f"\n--- [{s.label}] ({s.module})", flush=True)
        if out and out.strip():
            print(out.rstrip(), flush=True)
        if p.returncode != 0:
            print(f"FAILED: {s.label} (exit {p.returncode}, {dt:.0f}s)",
                  file=sys.stderr)
            if first_rc == 0:
                first_rc = p.returncode
        else:
            print(f"--- {s.label} done in {dt:.0f}s (parallel)", flush=True)
    return first_rc
