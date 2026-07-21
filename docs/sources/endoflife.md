# endoflife.date

Per-product release-cycle EOL dates. They mark a cpp **package** end-of-life
in `data/sources/cpp/eol.csv`. That file is advisory. A curator reads it and
sets the per-repo `eol` verdict in `data/eligibility/overrides.csv`, and that
verdict clears the repo's `active` flag (`active = NOT eol AND NOT archived`).

Small but reliable: it covers well-known products only, and a maintainer
publishes each cycle's EOL date.

## Data Sources

| Endpoint | Use |
|---|---|
| `https://endoflife.date/api/{product}.json` | release cycles for one product |

`src/sources/cpp/check_eol.py` holds `ENDOFLIFE_MAP`, a curated
`cpp_package → product slug` map. Only slugs verified against
`/api/all.json` are listed, so an unmapped package never queries the API.
bash, boost, git, jq, openssh and samba are not tracked upstream.

## Raw Data

`data/sources/endoflife/<product>.json` — one cached response per product.
The cache has no TTL; `--refresh` refetches it.

## How It Works

The model asks whether the **project** is dead, not whether a version is. A
package is EOL when the latest `eol` date across all cycles is in the past.
Any cycle with support life left keeps the project alive, and a cycle with
`eol: false` (an open-ended supported cycle) does the same. Non-date values
are ignored.

This is the second of two cpp EOL signals. Homebrew's `disable!` /
`deprecate!` formula flags run first; endoflife.date resolves the rest. See
[cpp](cpp.md) and [homebrew](homebrew.md).

## Scripts

| Script | Does |
|---|---|
| `src/sources/cpp/check_eol.py` | fetches cycles, applies both signals, writes `data/sources/cpp/eol.csv` |

```bash
uv run python -m src.sources.cpp.check_eol
uv run python -m src.sources.cpp.check_eol --refresh   # refetch caches
```

## Outputs

`data/sources/cpp/eol.csv` — `package, is_eol, eol_method, eol_reason, source,
eol_checked_at`. `eol_method` records which signal decided the row:

| `eol_method` | Meaning |
|---|---|
| `homebrew_disabled` | formula carries `disable!` — EOL |
| `homebrew_deprecated` | formula carries `deprecate!` — EOL |
| `homebrew_alive` | formula checked, neither flag set |
| `endoflife_alive` | cycles checked, support life remains |
| `unsupported` | no signal — never treated as EOL |

The file is **advisory**. `src.eligibility.build_active` does not read it. It
reads the manual per-repo `eol` column in `data/eligibility/overrides.csv`,
which a curator sets using this file as evidence. The distinction matters:
`eol.csv` flags *packages*, `overrides.csv` records a *repo* verdict. See
[eligibility](../eligibility.md).

## Limitations

- Covers cpp only. npm, PyPI and crates take EOL from their own registry
  deprecation and yank flags.
- The product map is curated by hand, so a new package needs a map entry
  before it can be checked.
- A product absent from endoflife.date is not EOL — it is unknown. The
  builder treats it as active.
