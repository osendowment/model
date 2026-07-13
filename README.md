# Open Source Endowment Fund Distribution Model

This is the work-in-progress fund distribution model for the [Open Source Endowment][ose].

## High-level Overview

### Principles

1. We aim to build a transparent, measurable, and verifiable model that can be iteratively improved by the open-source community and approved by a majority of active OSE donors.

2. It will never be a perfect model, because (1) open-source consumption cannot be measured with 100% precision, and (2) there is no ideal consensus on how to prioritize OSS grants.

### Ecosystems

We aim to focus our support on the core of open-source ecosystems — roughly the ~1% of packages that account for 99% of downloads and dependencies. Our model is a data-driven approximation of global open-source supply chain usage, designed to surface its most critical yet underfunded components.

It is important to trace dependencies across ecosystem boundaries, not just within them. For instance, Pandas [Python] depends on NumPy [Python], which depends on OpenBLAS [C] ([details](https://codeberg.org/vladh/bindep)). This cross-ecosystem view naturally elevates low-level infrastructure libraries in C/C++, Fortran, and similar languages.

Four registries are covered: **npm** (JS/TS), **PyPI** (Python), **crates** (Rust), and **C/C++** (Debian + Homebrew, which have no single registry). Repos are resolved on both **GitHub and GitLab** (gitlab.com, salsa.debian.org, gitlab.gnome.org, gitlab.freedesktop.org, invent.kde.org, and other instances).

## The Pipeline

Three scoring stages, then a preview build and a health gate. Each stage narrows the set the next operates on:

```
value → risk → eligibility → preview → health
```

| Stage | Question | Output |
|---|---|---|
| **[Value](docs/value.md)** | How important is this project? | `value_score` (0–100) |
| **[Risk](docs/risk.md)** | How likely is it to fail? | `risk_score` (0–100, higher = riskier) |
| **[Eligibility](docs/eligibility.md)** | Can we actually fund it? | `eligible` (boolean) |
| **Preview** | Publish the result | `data/preview/preview.xlsx` |
| **Health** | Is the build self-consistent? | red check ⇒ the run aborts |

**All three scoring stages are automated.** Eligibility used to be a manual review; it is now a full stage, with the residual human judgment confined to curated override files (`data/*/overrides.csv`), never to LLM-generated data.

The final `score` is `sqrt(value_score × risk_score)` — an unnormalized geometric mean on the same 0–100 scale as its inputs, so **both** dimensions must be high. `priority` is a dense rank by `score` descending over eligible rows only.

### Running it

`scripts/run-pipeline.sh` is the **only** supported entry point — for the whole pipeline, one stage, or a resume from the middle. Never invoke the stage runners by hand: the preview stage is the one everyone forgets, which silently leaves `preview.xlsx` stale.

```bash
scripts/run-pipeline.sh                    # every stage (TTL-cached, ~90s warm)
scripts/run-pipeline.sh --offline          # pure-cache run, no network
scripts/run-pipeline.sh --stage risk       # one stage
scripts/run-pipeline.sh --from-stage risk  # that stage through to the end
scripts/run-pipeline.sh --list-stages
```

`health` runs last and aborts on failure — a red check is a bug, never noise.

### What each score is made of

**Value** — a pro-rata weighted blend; only the components present for a repo are summed and renormalized, so a repo missing one still lands on the same scale.

| Component | Weight | What it measures |
|---|---|---|
| `openssf_crit` | 60% | OpenSSF Criticality Score: commit cadence, contributor count, org diversity, dependents, issue activity. GitHub-only. |
| `eco_crit` | 20% | Whether ecosyste.ms lists the package as critical infrastructure. Covers GitHub **and** GitLab, so it is the importance signal GitLab repos still get. |
| `top_eco_pct` | 10% | PageRank *position* within the repo's strongest ecosystem. Downloads pick the top packages (95% of cumulative downloads), their dependency tree is fetched, and a download-personalized PageRank (α = 0.85) ranks every node. |
| `pr_score` | 10% | Cross-ecosystem dependency *mass*, complementing `top_eco_pct`'s position. |

**Risk** — geometric mean of four dimensions, each 0–100 with higher = riskier.

| Dimension | What it measures |
|---|---|
| `concentration` | Bus factor + HHI over the last 5 years, from a git clone's commit log (mailmap applied, bots dropped). Absolute scales, so a one-person repo pins 100. |
| `complexity` | `scc` lines of code + `lizard` max cyclomatic complexity at a year-pinned SHA. Big **and** gnarly ranks worse than either alone. |
| `security` | Worst-of the OpenSSF Scorecard (inverted) and a CVE score from OSV. Real CVEs are never masked by a good Scorecard. |
| `workload` | LOC, CVEs, and net-new issues **per active contributor**. |

**Eligibility** — a plain AND of four booleans, no weights. Ineligible repos stay in the table with the failing flag visible.

| Check | True when |
|---|---|
| `oss` | The repo's SPDX license is OSI-approved. |
| `intent` | The repo shows *any* funding signal (Sponsors, FUNDING.yml, funding.json, Open Collective, institutional host…). Propagates at the owner level. |
| `nonprofit` | No company host/owner backs it — company-backed projects are already resourced. |
| `active` | `NOT eol AND NOT archived`. No exemptions: a project whose canonical upstream is off GitHub is *repointed* there (glibc → sourceware, pixman → gitlab.freedesktop.org), never exempted. |

## Layout

`src/`, `data/`, and `docs/` all mirror the pipeline:

- `src/sources/<source>/` — everything that fetches or processes one external source; `src/{value,risk,eligibility}/` — the stage builders and their runners; `src/common/` — shared infrastructure.
- `data/sources/<source>/` — raw + intermediate fetched data; `data/{value,risk,eligibility}/` — stage outputs; `data/preview/` — the published workbook.
- `docs/` — one page per stage ([value](docs/value.md), [risk](docs/risk.md), [eligibility](docs/eligibility.md)) plus [`docs/data-sources.md`](docs/data-sources.md), with [`docs/sources/`](docs/sources/) (one page per data source) and [`docs/components/`](docs/components/) (cross-cutting components) beneath.

**Every count lives in one place.** Funnel, coverage, and distribution figures are on the `stats` sheet of `preview.xlsx`, regenerated from the live CSVs on every build. The methodology pages describe *how* a metric is built and never restate *how many* — so there is no stats document to drift.

## Auditability

The model must be traceable end to end: every metric in an output CSV traces back to the fetch that produced it. Every fetch records a date and a success flag, so a `False`/`0` can never silently stand in for a network error. Repo-keyed source files carry a stable `repo_id` (`gh/<numeric>` or `gl/<nickname>-<id>`), because slugs drift on renames and every downstream join is by id. `scripts/pipeline_health.py` enforces this, and it is the last stage of every run.

Work is currently happening in this repo and the following places:

* [bindep][bindep] ([@vladh][vlad.website]) — Strategies for finding binary dependencies
* [software-finder][software-finder] ([@jring-o][jring-o]) — PyPI to GitHub repository mapper

[bindep]: https://codeberg.org/vladh/bindep
[jring-o]: https://github.com/jring-o/software-finder
[ose]: https://endowment.dev
[software-finder]: https://github.com/jring-o/software-finder
[vlad.website]: https://vlad.website
