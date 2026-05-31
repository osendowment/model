"""Shared orchestration helper for pipeline runners.

A pipeline is an ordered list of `Step`s. `run_pipeline` runs each as
`python -m <module>` in a subprocess, honouring --from / --only /
--skip-fetch / --list. Steps flagged `pipeline=True` are themselves
orchestrators — they always run (so their non-fetch steps still execute)
and `--skip-fetch` is forwarded into them.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class Step:
    label: str              # short name, used by --from / --only
    module: str             # dotted module path, run via `python -m`
    fetch: bool = False     # slow raw-data fetch step — skipped by --skip-fetch
    pipeline: bool = False  # step is itself an orchestrator — forward --skip-fetch


def select_steps(steps: list[Step], from_step: str | None,
                 only: str | None, skip_fetch: bool) -> list[Step]:
    """Resolve --from / --only / --skip-fetch into the list of steps to run.

    A `pipeline` step is kept even under --skip-fetch (its own fetch steps
    are skipped instead, via the forwarded flag); a plain `fetch` step is
    dropped. Raises KeyError if `from_step` / `only` names an unknown step.
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
    if skip_fetch:
        chosen = [s for s in chosen if s.pipeline or not s.fetch]
    return chosen


def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--from", dest="from_step", metavar="STEP",
                   help="Start from this step, skipping earlier ones")
    p.add_argument("--only", metavar="STEP", help="Run only this step")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Skip slow raw-data fetch steps")
    p.add_argument("--list", action="store_true", help="List steps and exit")
    return p


def run_pipeline(steps: list[Step], args: argparse.Namespace) -> int:
    """Execute the selected steps as subprocesses. Returns an exit code."""
    if args.list:
        for s in steps:
            tags = " ".join(t for t, on in
                            (("[fetch]", s.fetch), ("[pipeline]", s.pipeline)) if on)
            print(f"  {s.label:24s} {s.module}  {tags}".rstrip())
        return 0
    try:
        selected = select_steps(steps, args.from_step, args.only, args.skip_fetch)
    except KeyError as e:
        print(f"unknown step: {e.args[0]}", file=sys.stderr)
        return 2
    for s in selected:
        print(f"\n=== {s.label} ({s.module}) ===", flush=True)
        cmd = [sys.executable, "-m", s.module]
        if s.pipeline and args.skip_fetch:
            cmd.append("--skip-fetch")
        t0 = time.monotonic()
        result = subprocess.run(cmd)
        dt = time.monotonic() - t0
        if result.returncode != 0:
            print(f"FAILED: {s.label} (exit {result.returncode}, {dt:.0f}s)",
                  file=sys.stderr)
            return result.returncode
        print(f"--- {s.label} done in {dt:.0f}s", flush=True)
    return 0
