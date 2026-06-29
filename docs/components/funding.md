# Funding (risk component)

How well-resourced is a project? The funding component gathers every public
signal that a repo receives (or gives) financial support — GitHub Sponsors,
`FUNDING.yml` platforms, the FLOSS Fund directory, OpenCollective budgets, and
FOSS-foundation hosting — and distills them into:

1. a **funding-risk score (`score`, 0–100, higher = more at-risk)** stored in
   `data/risk/funding.csv` (not carried into `risk.csv` — see below), and
2. two boolean flags — **`intent`** and **`nonprofit`** — that are joined into
   `risk.csv`.

Scope: the class-A value-class repos in the risk pipeline (counts in
[stats.md → Risk → Intent and nonprofit](../stats.md#intent-and-nonprofit);
see [value.md](../value.md)).
Build step: `src/risk/build_funding.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[most recent]` = the latest pull of that source; `[2021–2025]` = a 5-year
window. Raw signals are fetched per-source under `data/sources/`; derived
columns are computed by `build_funding.py`.

```
Funding  → data/risk/funding.csv  (one row per class-A risk repo)
│
├── GitHub Sponsors
│   ├── gh_sponsors_in        ← GraphQL sponsorshipsAsMaintainer (owner + FUNDING.yml github logins)  [most recent]
│   ├── gh_sponsors_out       ← GraphQL sponsorshipsAsSponsor   (repo owner account)                  [most recent]
│   ├── gh_sponsorships       ← derived (in + out, total engagement)                                  [most recent]
│   └── gh_sponsorships_p     ← derived (worst-pinned CDF risk percentile of gh_sponsorships)         [most recent]
│
├── FUNDING.yml  (.github/FUNDING.yml)
│   ├── has_funding_yml       ← REST contents API                                                     [most recent]
│   ├── funding_yml_platforms ← derived (declared platform keys)                                      [most recent]
│   └── <platform handles>    ← parsed handles: github, patreon, open_collective, tidelift, custom …  [most recent]
│
├── FLOSS Fund  (funding.json)
│   └── has_funding_json      ← dir.floss.fund directory export ∩ repo (URL match)                    [most recent]
│
├── OpenCollective
│   ├── oc_avg_funding        ← OC GraphQL totalAmountReceived (gross, mean of years; $0 if none)     [2021–2025]
│   └── oc_avg_funding_p      ← derived (worst-pinned CDF risk percentile of oc_avg_funding)          [2021–2025]
│
├── Institutional backing  (funding/host-by-repo.csv ∪ funding/overrides.csv)
│   ├── host, host_type      ← legally-connected steward domain (e.g. apache.org, react.foundation)  [most recent]
│   ├── owner, owner_type     ← owning-entity domain (e.g. meta.com), from overrides.csv               [most recent]
│   └── host_score           ← derived: combined backing, most-funded of host/owner (0 · 0.5 · 1)     [most recent]
│
├── channels_count           ← derived (FUNDING.yml platforms ∪ funding.json channels, deduped)      [most recent]
├── gh_stars, gh_forks        ← GitHub /repos (informational, not scored)                             [most recent]
│
├── score  (funding-risk score)  ← derived (geom-mean of gh_sponsorships_p, oc_avg_funding_p, host_score×100; int 1–100)  [most recent]
│                                  (NOT carried into risk.csv — funding is not a scored dimension)
├── intent                     ← derived bool: ≥1 funding signal present (see below)
└── nonprofit                  ← derived bool: false only when host_type or owner_type == company
```

## How It Works

1. **Collect** — five fetchers pull raw funding signals into `data/sources/`,
   each TTL-controlled so re-runs only fetch what's missing or stale.
2. **Join** — `build_funding.py` joins the sources onto the risk repos
   (by `repo`, by owner `login` for outbound sponsoring, by OC `slug`).
3. **Derive** — combine raw signals (`gh_sponsorships`, `channels_count`,
   `has_funding_json`) and compute the percentiles.
4. **Score** — `score` = geometric mean of the two channel percentiles, then
   **× `host_score`** — the combined backing multiplier (most-funded of host /
   owner: company 0 · nonprofit 0.5 · none 1); integer 0–100, higher = more at-risk.
5. **Flags** — `aggregate_risk.py` derives `intent` and `nonprofit` booleans
   from the funding signals and joins them into `risk.csv` alongside the four
   scored dimensions. The funding `score` does **not** feed `risk.csv`; funding
   is a signal-only component, not a scored dimension.

Pipeline order (`src/risk/run_risk_pipeline.py`). The risk runner fetches these
sources by default (incremental — each fetcher skips data already present, so a
re-run only fills gaps); pass `--skip-fetch` to rebuild from existing data without
fetching:

```
funding-yml → sponsors → sponsorships → floss-fund → opencollective → funding-build
```

## Collection

Five sources feed the build. Each fetcher records a `*_status` and/or
`fetched_at`, so a `0`/`False` value is distinguishable from a failed fetch.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `github/sponsors.csv` | `src/sources/github/fetch_sponsors.py` | inbound GitHub Sponsors count | `repo` |
| `github/sponsorships.csv` | `src/sources/github/fetch_sponsorships.py` | outbound sponsoring count | `login` |
| `github/funding-yml.csv` | `src/sources/github/fetch_funding_yml.py` | `.github/FUNDING.yml` platforms + handles | `repo` |
| `floss-fund/funding-json.csv` | `src/sources/floss_fund/funding_json.py` | FLOSS Fund manifest directory | `id` |
| `opencollective/budgets.csv` | `src/sources/opencollective/fetch_budgets.py` | OC gross annual budgets | `slug` |
| `funding/host-by-repo.csv` | foundations scrapers (`src/sources/funding/`) | scraped FOSS-foundation host | `repo` |
| `funding/overrides.csv` | curated | per-repo `host`/`owner` **domains** + types (company/nonprofit); schema `repo,host,host_type,gh_user,owner,owner_type` | `repo` |

`gh_stars` / `gh_forks` are read from `data/sources/github/repos.csv` (GitHub
`/repos`) and carried as informational columns — they are **not** scored.

### GitHub Sponsors — inbound vs outbound

GitHub sponsorship has two directions, and they mean opposite things:

| Metric | GraphQL field | Meaning |
|---|---|---|
| `gh_sponsors_in` | `sponsorshipsAsMaintainer` | accounts **sponsoring** this repo's owner (+ `github:` logins in FUNDING.yml) |
| `gh_sponsors_out` | `sponsorshipsAsSponsor` | accounts the repo's **owner sponsors** (the org's "Sponsoring N") |

`gh_sponsors_out` is an account-level property, so `sponsorships.csv` is keyed
by `login` (~412 owner logins, ~half the per-repo count) and gap-fills only new
owners. A repo whose owner sponsors others (e.g. `astral-sh`, "Sponsoring 39")
is a *resourced* backer, not an unfunded project — which is why the score uses
**`in + out`** rather than `in − out`.

### funding.json from the FLOSS Fund directory

Rather than fetch `funding.json` from ~900 repos individually (6 in-scope hits),
`funding_json.py` downloads the whole [FLOSS Fund](https://dir.floss.fund)
directory once and `build_funding` matches `project_repository` URLs against the
risk repos — **6 in-scope hits** with zero per-repo requests. The export also
parses each manifest's channels into per-platform handles + `channel_platforms`.

### OpenCollective budgets

For every `open_collective` handle declared in `funding-yml.csv` or the FLOSS
Fund export (in-scope only), `fetch_budgets.py` queries the OC GraphQL API for
`totalAmountReceived` per calendar year 2021–2025 (gross incoming). The API
rate-limits hard unauthenticated (HTTP 429); set `OPENCOLLECTIVE_PERSONAL_TOKEN`
in `.env` to lift it. `oc_avg_funding` is the mean over years with data.

## Processing & scoring

### Derived signals

| Column | Formula |
|---|---|
| `gh_sponsorships` | `gh_sponsors_in + gh_sponsors_out` |
| `channels_count` | distinct platforms across FUNDING.yml ∪ funding.json |
| `has_funding_json` | repo URL present in the FLOSS Fund export |
| `oc_avg_funding` | mean of OC `raised_*` years (**`0`** when no OC presence) |

### The percentiles (`_p`)

Each funding channel is turned into a **worst-pinned CDF risk percentile** —
lower funding ranks *higher* (more at-risk), mirroring the negated
`openssf_score` in the security component. Both are computed over all the top
repos (`gh_sponsorships` defaults to 0, `oc_avg_funding` defaults to $0).

| Column | Basis | Direction |
|---|---|---|
| `gh_sponsorships_p` | `gh_sponsorships` (in + out) | low engagement → high percentile |
| `oc_avg_funding_p` | `oc_avg_funding` | low $ → high percentile |
| **`score`** | geom-mean of **three** axes: the two `_p` **and `host_score×100`** | the funding-risk score (int 1–100) |

```
score      = max(1, round( ∛(gh_sponsorships_p × oc_avg_funding_p × host_score×100) ))
host_score = min( type(host), type(owner) )      # 0 company · 0.5 nonprofit · 1 none
```

`host_score` (0/0.5/1) enters the geom mean **scaled to the 0–100 axis** (×100:
company 0 · nonprofit 50 · none 100) so it is commensurate with the two channel
percentiles — one of three equal voices rather than a blunt multiplier.

The **geometric mean** is the key choice: a repo funded strongly on *either*
channel gets a low (good) `score`, because one low percentile pulls the
product down. A project with no GitHub Sponsors but a healthy OpenCollective
(or vice-versa) is correctly read as funded.

The **`host_score`** then folds in institutional resourcing the GitHub/OC axes
miss. `host` is the foundation/company **legally** stewarding the project (a
domain — only a *legally connected* steward counts, not a loose community
association); `owner` is the owning entity (a domain). Each is classified
**company** (0 — fully resourced, score floors at 1), **nonprofit/foundation**
(0.5 — halved), or **none** (1 — unchanged). `host_score` is the **most-funded
of the two** (`min`), so a single value ∈ {0, 0.5, 1}:

- A scraped FOSS-foundation host (`funding/host-by-repo.csv`) defaults to
  nonprofit, so Apache/LF/CNCF/NumFOCUS/PSF repos drop from 100 → 50 as before.
- A curated `funding/overrides.csv` row sets either side by domain.
  `facebook/react` (host `react.foundation` nonprofit, owner `meta.com` company)
  → host_score `min(0.5, 0)` = 0 → ∛(100·100·0) = **1**; `rust-lang/rust` (host
  `rustfoundation.org` nonprofit, no owner) → host_score `min(0.5, 1)` = 0.5 →
  ∛(100·100·50) = **79**.

Non-backed unfunded repos stay at 100.

Worked examples:

| Repo | oc_avg | ships | host_score | `score` | Reading |
|---|---:|---:|---:|---:|---|
| facebook/react | 0 | 0 | 0 | **1** | company-owned (meta.com) — floors at 1 |
| vuejs/core | $132,860 | 158 | 1 | 5 | funded both channels, no institutional backer |
| axios/axios | $32,031 | 20 | 1 | 10 | funded both, no backer |
| zloirock/core-js | $34,530 | 0 | 1 | 13 | OC-funded only |
| rust-lang/rust | 0 | 0 | 0.5 | 79 | nonprofit foundation host only |
| acornjs/acorn | 0 | 0 | 1 | **100** | no funding, no backer |

Note the third axis lifts *funded-but-unbacked* repos (vuejs `1 → 5`, axios
`3 → 10`): "no institutional backer" (`host_score = 1` → backing 100) is now a
risk voice, not a no-op. The cohort median rose 69 → 78.

## Output

### `data/risk/funding.csv` (per-dimension build)

20 columns, one row per risk repo. No `fetched_at` — per-signal timestamps stay
in each source file.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `gh_sponsors_in` | inbound GitHub Sponsors count |
| `gh_sponsors_out` | outbound sponsoring count (owner) |
| `gh_sponsorships` | `in + out` |
| `gh_sponsorships_p` | risk percentile of `gh_sponsorships` |
| `gh_stars`, `gh_forks` | GitHub stars / forks (informational, not scored) |
| `has_funding_yml` | repo has `.github/FUNDING.yml` |
| `funding_yml_platforms` | declared platform keys (comma-sep) |
| `has_funding_json` | registered in the FLOSS Fund directory |
| `channels_count` | distinct funding platforms |
| `oc_avg_funding` | mean OC gross annual budget (`0` if none) |
| `oc_avg_funding_p` | risk percentile of `oc_avg_funding` |
| `host` | legally-connected steward **domain** (e.g. `apache`, `react.foundation`) or empty |
| `host_type` | `company` / `nonprofit` / empty |
| `owner` | owning-entity **domain** (e.g. `meta.com`), from `overrides.csv` |
| `owner_type` | `company` / `nonprofit` / empty |
| `host_score` | combined backing = `min(type(host), type(owner))` ∈ {`0` company, `0.5` nonprofit, `1` none} — multiplies `score` |
| `score` | **funding-risk score** (geom-mean of the two `_p` × `host_score`, int 1–100) |

### `data/risk/risk.csv` (aggregate)

The funding component does **not** contribute a scored column to `risk.csv`.
The overall `risk.csv` `score` is the geometric mean of the **four** scored
dimensions only: `concentration`, `complexity`, `security`, `workload`.

Instead, `aggregate_risk.py` derives two boolean flag columns from the funding
signals and joins them into `risk.csv`:

| `risk.csv` column | Source | Default |
|---|---|---|
| `intent` | `true` when the repo has ≥1 funding signal (see below) | `false` |
| `nonprofit` | `false` only when `host_type`/`owner_type == company` | `true` |

### `intent` and `nonprofit` flags

**`intent`** (`bool`, default `false`) — `true` when the repo shows at least
one funding signal: GitHub Sponsors (inbound or outbound), a `.github/FUNDING.yml`,
a `funding.json` (FLOSS Fund), an npm `funding` field, a PyPI project-URLs
funding entry, an Open Collective slug, or an institutional host or owner.
"Intent" means the project is actively seeking or accepting support.

**`nonprofit`** (`bool`, default `true`) — `false` only when a corporate entity
(Meta, Google, Microsoft, AWS, …) is the project's host or owner, as determined by
`host_type` / `owner_type == "company"` in `data/risk/funding.csv`.
Community-run and foundation-backed projects are `nonprofit=true`.
Corporate-backed repos remain in `risk.csv` with their scores intact; callers
filter by `nonprofit=true` to restrict to endowment candidates.

## Coverage

See [stats.md → Risk → Intent and nonprofit](../stats.md#intent-and-nonprofit)
for current per-channel coverage over the top repos and the score distribution.

## Limitations

- **Still GitHub/OC-centric for $ signal.** The two *scored axes* are inbound/
  outbound GitHub Sponsors and OpenCollective. **Institutional backing is now
  folded in** (the `host_score` factor — scraped foundations plus curated
  legally-connected company/foundation overrides), but a repo with no `host`/`owner`
  override and no scraped foundation host still shows little off-platform signal
  (corporate payroll, private grants, VC) — except via
  the outbound-sponsoring proxy. `astral-sh/ruff` is the canonical case: VC-backed
  yet `gh_sponsors_in = 0`, landing at `score` 48 only because its outbound signal
  flags it as a well-resourced backer.
- **funding.json is still negligible** (6 repos) — the structured-manifest
  ecosystem hasn't reached this cohort. `has_funding_json` is informational, not
  yet a scoring input.
- **`score` is a percentile, not a class.** It's a 0–100 risk number, not a
  class tier. It lives in `funding.csv` and is **not** folded into the overall
  `risk.csv` `score` — the risk aggregate uses four scored dimensions only
  (concentration, complexity, security, workload).
- **OC is the only $ amount.** GitHub Sponsors and Patreon/Tidelift amounts
  aren't public, so dollar figures exist only for the ~45 OpenCollective repos;
  the sponsorship axis is a *count*, not a *sum*.
