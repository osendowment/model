# Funding (risk component)

How well-resourced is a project? The funding component gathers every public
signal that a repo receives (or gives) financial support — GitHub Sponsors,
`FUNDING.yml` platforms, the FLOSS Fund directory, OpenCollective budgets, and
FOSS-foundation hosting — and distills them into one **funding-risk percentile
(`funding_p`)** that feeds `data/risk/risk.csv`.

Scope: the 860 A/B value-class repos in the risk pipeline (see
[value.md](../value.md)). Build step: `src/pipeline/risk/build_funding.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[most recent]` = the latest pull of that source; `[2021–2025]` = a 5-year
window. Raw signals are fetched per-source under `data/sources/`; derived
columns are computed by `build_funding.py`.

```
Funding  → data/risk/funding.csv  (860 A/B risk repos)
│
├── GitHub Sponsors
│   ├── gh_sponsors_in        ← GraphQL sponsorshipsAsMaintainer (owner + FUNDING.yml github logins)  [most recent]
│   ├── gh_sponsors_out       ← GraphQL sponsorshipsAsSponsor   (repo owner account)                  [most recent]
│   ├── gh_sponsorships       ← derived (in + out, total engagement)                                  [most recent]
│   └── gh_sponsorships_p     ← derived (inverted Hazen percentile of gh_sponsorships)                [most recent]
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
│   └── oc_avg_funding_p      ← derived (inverted Hazen percentile of oc_avg_funding)                 [2021–2025]
│
├── Foundations
│   └── foundation_host       ← foundations/host-by-repo.csv (apache, lf, psf, numfocus …)            [most recent]
│
├── channels_count           ← derived (FUNDING.yml platforms ∪ funding.json channels, deduped)      [most recent]
│
└── funding_p  (the score)   ← derived (geometric mean of gh_sponsorships_p, oc_avg_funding_p)        [most recent]
    └─ carried into risk.csv together with oc_avg_funding + gh_sponsorships
```

## How It Works

1. **Collect** — five fetchers pull raw funding signals into `data/sources/`,
   each TTL-controlled so re-runs only fetch what's missing or stale.
2. **Join** — `build_funding.py` joins the sources onto the 860 risk repos
   (by `repo`, by owner `login` for outbound sponsoring, by OC `slug`).
3. **Derive** — combine raw signals (`gh_sponsorships`, `channels_count`,
   `has_funding_json`) and compute the percentiles.
4. **Score** — `funding_p` = geometric mean of the two channel percentiles.
5. **Aggregate** — `aggregate_risk.py` carries `oc_avg_funding`,
   `gh_sponsorships`, and `funding_p` into `risk.csv`.

Pipeline order (`src/pipeline/run_risk_pipeline.py`, fetchers run with
`--with-fetchers`):

```
funding-yml → sponsors → sponsorships → floss-fund → opencollective → funding-build
```

## Collection

Five sources feed the build. Each fetcher records a `*_status` and/or
`fetched_at`, so a `0`/`False` value is distinguishable from a failed fetch.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `github/sponsors.csv` | `src/github/fetch_sponsors.py` | inbound GitHub Sponsors count | `repo` |
| `github/sponsorships.csv` | `src/github/fetch_sponsorships.py` | outbound sponsoring count | `login` |
| `github/funding-yml.csv` | `src/github/fetch_funding_yml.py` | `.github/FUNDING.yml` platforms + handles | `repo` |
| `floss-fund/funding-json.csv` | `src/floss_fund/funding_json.py` | FLOSS Fund manifest directory | `id` |
| `opencollective/budgets.csv` | `src/opencollective/fetch_budgets.py` | OC gross annual budgets | `slug` |
| `foundations/host-by-repo.csv` | foundations pipeline | FOSS-foundation host | `repo` |

### GitHub Sponsors — inbound vs outbound

GitHub sponsorship has two directions, and they mean opposite things:

| Metric | GraphQL field | Meaning |
|---|---|---|
| `gh_sponsors_in` | `sponsorshipsAsMaintainer` | accounts **sponsoring** this repo's owner (+ `github:` logins in FUNDING.yml) |
| `gh_sponsors_out` | `sponsorshipsAsSponsor` | accounts the repo's **owner sponsors** (the org's "Sponsoring N") |

`gh_sponsors_out` is an account-level property, so `sponsorships.csv` is keyed
by `login` (412 owner logins, ~half the per-repo count) and gap-fills only new
owners. A repo whose owner sponsors others (e.g. `astral-sh`, "Sponsoring 39")
is a *resourced* backer, not an unfunded project — which is why the score uses
**`in + out`** rather than `in − out`.

### funding.json from the FLOSS Fund directory

Rather than fetch `funding.json` from ~900 repos individually (1 in-scope hit),
`funding_json.py` downloads the whole [FLOSS Fund](https://dir.floss.fund)
directory once and `build_funding` matches `project_repository` URLs against the
risk repos — **5 in-scope hits** with zero per-repo requests. The export also
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

Each funding channel is turned into an **inverted Hazen risk percentile** —
lower funding ranks *higher* (more at-risk), mirroring the negated
`openssf_score` in the security component. Both are computed over all 860 repos
(`gh_sponsorships` defaults to 0, `oc_avg_funding` defaults to $0).

| Column | Basis | Direction |
|---|---|---|
| `gh_sponsorships_p` | `gh_sponsorships` (in + out) | low engagement → high percentile |
| `oc_avg_funding_p` | `oc_avg_funding` | low $ → high percentile |
| **`funding_p`** | geometric mean of the two | the funding-risk score |

The **geometric mean** is the key choice: a repo funded strongly on *either*
channel gets a low (good) `funding_p`, because one low percentile pulls the
product down. A project with no GitHub Sponsors but a healthy OpenCollective
(or vice-versa) is correctly read as funded.

Worked examples:

| Repo | oc_avg | ships | `funding_p` | Reading |
|---|---:|---:|---:|---|
| vuejs/core | $132,860 | 158 | **0.8** | funded both channels |
| axios/axios | $32,031 | 20 | 3.0 | funded both |
| zloirock/core-js | $34,530 | 0 | 3.6 | OC-funded only — still low risk |
| astral-sh/ruff | 0 | 39 | 35.4 | VC-backed, sponsors others |
| acornjs/acorn | 0 | 0 | **63.5** | no detectable funding |

## Output

### `data/risk/funding.csv` (per-dimension build)

14 columns, one row per risk repo. No `fetched_at` — per-signal timestamps stay
in each source file.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `gh_sponsors_in` | inbound GitHub Sponsors count |
| `gh_sponsors_out` | outbound sponsoring count (owner) |
| `gh_sponsorships` | `in + out` |
| `gh_sponsorships_p` | inverted percentile of `gh_sponsorships` |
| `has_funding_yml` | repo has `.github/FUNDING.yml` |
| `funding_yml_platforms` | declared platform keys (comma-sep) |
| `has_funding_json` | registered in the FLOSS Fund directory |
| `channels_count` | distinct funding platforms |
| `oc_avg_funding` | mean OC gross annual budget (`0` if none) |
| `oc_avg_funding_p` | inverted percentile of `oc_avg_funding` |
| `funding_p` | **funding-risk score** (geom-mean of the two `_p`) |
| `foundation_host` | FOSS-foundation host (e.g. `apache`, `psf`) |

### `data/risk/risk.csv` (aggregate)

The funding dimension contributes only its three headline columns —
`aggregate_risk.py` whitelists them and drops everything else (no
`funding_fetched_at`):

| Column | Why it's carried |
|---|---|
| `gh_sponsorships` | raw GitHub-sponsorship engagement |
| `oc_avg_funding` | raw OpenCollective $/yr |
| `funding_p` | the composite funding-risk percentile |

## Coverage

Of the 860 A/B risk repos:

| Signal | Repos | % |
|---|---:|---:|
| GitHub Sponsors (inbound > 0) | 435 | 50.6% |
| FUNDING.yml present | 253 | 29.4% |
| ≥ 1 funding channel | 255 | 29.7% |
| Owner sponsors others (out > 0) | 158 | 18.4% |
| OpenCollective budget > 0 | 45 | 5.2% |
| Foundation host | 36 | 4.2% |
| funding.json (FLOSS Fund) | 5 | 0.6% |

`funding_p` percentiles: p25 **32.6** · p50 **50.3** · p75 **63.5**. The mass
of unfunded repos (no sponsors, no OC) tie near the top — `funding_p` ≈ 63.5 is
the "no detectable funding" plateau.

## Limitations

- **GitHub-centric.** Inbound sponsors, outbound sponsoring, and FUNDING.yml all
  come from GitHub. A project funded entirely off-platform (corporate payroll,
  grants, a private foundation) shows little signal — `astral-sh/ruff` is the
  canonical case: VC-backed yet `gh_sponsors_in = 0`. The outbound signal
  partly compensates by flagging well-resourced backers.
- **funding.json is still negligible** (5 repos) — the structured-manifest
  ecosystem hasn't reached this cohort. `has_funding_json` is informational, not
  yet a scoring input.
- **`funding_p` is not a class.** It's a percentile, not an A–D class, and it is
  **not yet folded into a composite risk score** — it sits alongside the other
  per-dimension percentiles in `risk.csv` for downstream use.
- **OC is the only $ amount.** GitHub Sponsors and Patreon/Tidelift amounts
  aren't public, so dollar figures exist only for the ~45 OpenCollective repos;
  the sponsorship axis is a *count*, not a *sum*.
