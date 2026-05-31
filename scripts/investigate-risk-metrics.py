#!/usr/bin/env python3
"""One-off: investigate largest / smallest / missing risk metrics.

Scans the narrow aggregate `data/risk/risk.csv` and every per-dimension
detail CSV under `data/risk/` (`complexity`, `concentration`, `security`,
`funding`, `workload`). For each numeric column it reports the top-5 and
bottom-5 repos by value, the missing count, and flags anomalies (negative
values where they shouldn't be, duplicate columns).

Numeric columns are auto-detected (year-agnostic schema), so this keeps
working as columns are renamed.

    uv run python scripts/investigate-risk-metrics.py
"""

import csv
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "risk"
CSVS = ["risk", "complexity", "concentration", "security", "funding", "workload"]

# Columns where a negative value is legitimate (trends / deltas / slopes).
SIGNED_SUFFIXES = ("net_new_issues_5y", "slope_opened", "slope_closed",
                   "issue_trend_score")
# Non-metric identity/text columns to skip even if they look numeric.
SKIP = {"repo", "repo_id"}


def _numeric_columns(header: list[str], rows: list[dict]) -> list[str]:
    """A column is numeric if >=80% of its non-empty cells parse as float."""
    out = []
    for col in header:
        if col in SKIP:
            continue
        nonempty = [(r.get(col) or "").strip() for r in rows if (r.get(col) or "").strip()]
        if not nonempty:
            continue
        parsed = 0
        for v in nonempty:
            try:
                float(v)
                parsed += 1
            except ValueError:
                pass
        if parsed >= 0.8 * len(nonempty):
            out.append(col)
    return out


def investigate(name: str) -> None:
    path = DATA / f"{name}.csv"
    if not path.exists():
        print(f"== {name}.csv: NOT FOUND ==\n")
        return
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [dict(zip(header, r)) for r in reader]

    total = len(rows)
    print(f"== {name}.csv: {total} rows, {len(header)} columns ==")

    dupes = [c for c, n in Counter(header).items() if n > 1]
    if dupes:
        print(f"  !! DUPLICATE COLUMNS: {dupes}")

    for col in _numeric_columns(header, rows):
        vals = []  # (value, repo)
        for r in rows:
            raw = (r.get(col) or "").strip()
            if raw == "":
                continue
            try:
                vals.append((float(raw), r.get("repo", "?")))
            except ValueError:
                print(f"  !! {col}: unparseable value {raw!r} in {r.get('repo', '?')}")
        missing = total - len(vals)
        if not vals:
            print(f"  {col}: ALL MISSING ({missing}/{total})")
            continue
        vals.sort()
        signed = col.endswith(SIGNED_SUFFIXES)
        neg = [v for v in vals if v[0] < 0]
        flag = f"  !! {len(neg)} NEGATIVE" if (neg and not signed) else ""
        print(f"  {col}: missing {missing}/{total}{flag}")
        print("    smallest:", ", ".join(f"{v:g}@{r}" for v, r in vals[:5]))
        print("    largest :", ", ".join(f"{v:g}@{r}" for v, r in vals[-5:]))
        if neg and not signed:
            print("    negatives:", ", ".join(f"{v:g}@{r}" for v, r in neg[:10]))
    print()


def main() -> None:
    for name in CSVS:
        investigate(name)


if __name__ == "__main__":
    main()
