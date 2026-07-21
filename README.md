# Open Source Endowment Fund Distribution Model

This is the work-in-progress model used by the [Open Source Endowment](https://endowment.dev/) to determine target open-source projects for its grant program.

It collects data on open-source software from [20+ data sources](docs/data-sources.md), calculates their Value and Risk scores, checks Eligibility factors, and provides the OSE board of directors with preview results for the final due diligence and grant approval.

### Principles

1. We are building a transparent, measurable, and verifiable model that can be iteratively improved by the open-source community and approved by the OSE members.

2. We focus our support on the critical core of open-source ecosystems — roughly the 1% of projects that account for 99% of downloads and dependencies.

3. Our model is a data-driven approximation of global open-source supply chain usage, designed to find the riskiest of its most valuable components.

4. This model will never be perfect, because open-source consumption cannot be precisely measured, and there is no ideal consensus on how to prioritize OSS grants.


## Model

Value narrows the open-source universe to a core set of projects. Risk and Eligibility then score and flag that core. The fourth step is human-based due diligence and approval by the OSE board.

| VALUE | RISK | ELIGIBILITY | DUE DILIGENCE |
|:---|:---|:---|:---|
| Downloads<br>Dependencies<br>Dependents<br>Criticality | Concentration<br>Complexity<br>Security<br>Workload | Open source<br>Funding intent<br>Nonprofit<br>Active | Double-check eligibility<br>Contact project leaders<br>Final board approval |
| **Defines the "core" projects** | **Finds the riskiest repos within the core** | **Flags ineligible repos within the core** | **Manual checks and board approval** |

Downloads, dependencies and dependents combine into a weighted PageRank. Its cumulative share defines the core — a power-law distribution, where a small head of projects carries almost all the dependency mass. Criticality then ranks projects *within* the core; it does not decide who is in it.

### Limitations

Currently, the model aggregates data on packages from four ecosystems:

| Language | Registries |
|---|---|
| JavaScript/TypeScript | [npmjs.com](https://www.npmjs.com/) |
| Python | [pypi.org](https://pypi.org/) |
| Rust | [crates.io](https://crates.io/) |
| C/C++ | [debian.org](https://www.debian.org/), [brew.sh](https://brew.sh/) |

The most valuable packages (based on downloads and dependency graph) are linked with corresponding repos, which are later used to collect risk and eligibility data points for scoring.

The model currently resolves only repos hosted on:

* GitHub
* GitLab, including custom hosts

Most package managers store clean historical data. The C/C++ ecosystem does not, so the model has to use approximations.

## Data Pipeline


| Stage | Question | Output |
|---|---|---|
| **[Value](docs/value.md)** | How important is this project? | `value_score` (0–100) |
| **[Risk](docs/risk.md)** | How likely is it to fail? | `risk_score` (0–100, higher = riskier) |
| **[Eligibility](docs/eligibility.md)** | Can we actually fund it? | `eligible` (boolean) |
| **[Preview](data/preview/preview.xlsx)** | Publish the result | [`data/preview/preview.xlsx`](data/preview/preview.xlsx) |


**All three scoring stages are automated.**

The final `score` is `sqrt(value_score × risk_score)` — an unnormalized geometric mean on the same 0–100 scale as its inputs, so **both** dimensions must be high. `eligible` is used to flag ineligible projects in the preview results. `priority` is a dense rank by `score` descending over potentially eligible projects only.

### Running it

`scripts/run-pipeline.sh` is the **only** supported entry point — for the whole pipeline, one stage, or a resume from the middle. Never invoke the stage runners by hand: the preview stage is the one everyone forgets, which silently leaves `preview.xlsx` stale.

```bash
scripts/run-pipeline.sh                    # every stage (TTL-cached, ~90s warm)
scripts/run-pipeline.sh --offline          # pure-cache run, no network
scripts/run-pipeline.sh --stage risk       # one stage
scripts/run-pipeline.sh --from-stage risk  # that stage through to the end
scripts/run-pipeline.sh --list-stages
```

The `health` check runs last to ensure pipeline data consistency, and aborts on failure.

### What each score is made of

**Value** — a pro-rata weighted blend; only the components present for a repo are summed and renormalized, so a repo missing one still lands on the same scale.

| Component | Weight | What it measures |
|---|---|---|
| `openssf_crit` | 60% | OpenSSF Criticality Score: commit cadence, contributor count, org diversity, dependents, issue activity. GitHub-only. |
| `eco_crit` | 20% | Whether ecosyste.ms lists the package as critical infrastructure. Covers GitHub **and** GitLab, so it is the importance signal GitLab repos still get. |
| `top_eco_pct` | 10% | Weighted PageRank *position* within the repo's strongest ecosystem. Downloads pick the top packages (95% of cumulative downloads), their dependency tree is fetched, and a download-personalized PageRank (α = 0.85) ranks every node. |
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
| `oss` | The repo carries an open-source license — OSI-approved, FSF-libre, or a curated equivalent. |
| `intent` | The repo shows *any* intent to be funded (GitHub Sponsors, FUNDING.yml, funding.json, Open Collective, institutional host, etc.) — OSE supports only those who want to be supported. Propagates at the repo owner level. |
| `nonprofit` | No company is strongly affiliated with the project — OSE supports only nonprofit initiatives. |
| `active` | `NOT eol AND NOT archived` — OSE supports only projects that still have work ahead of them. |

## Layout

`src/`, `data/`, and `docs/` all mirror the pipeline:

- `src/sources/<source>/` — everything that fetches or processes one external source; `src/{value,risk,eligibility}/` — the stage builders and their runners; `src/common/` — shared infrastructure.
- `data/sources/<source>/` — raw + intermediate fetched data; `data/{value,risk,eligibility}/` — stage outputs; `data/preview/` — the published workbook.
- `docs/` — one page per stage ([value](docs/value.md), [risk](docs/risk.md), [eligibility](docs/eligibility.md)) plus [`docs/data-sources.md`](docs/data-sources.md), with [`docs/sources/`](docs/sources/) (one page per data source) and [`docs/components/`](docs/components/) (cross-cutting components) beneath.

**Every count lives in one place.** Funnel, coverage, and distribution figures are on the `pipeline` sheet of [`preview.xlsx`](data/preview/preview.xlsx), regenerated from the live CSVs on every build. The methodology pages describe *how* a metric is built and never restate *how many* — so there is no stats document to drift.

## Auditability

The model must be traceable end to end: every metric in an output CSV traces back to the fetch that produced it. Every fetch records a date and a success flag, so a `False`/`0` can never silently stand in for a network error. Repo-keyed source files carry a stable `repo_id` (`gh/<numeric>` or `gl/<nickname>-<id>`), because slugs drift on renames and every downstream join is by id. `scripts/pipeline_health.py` enforces this, and it is the last stage of every run.

## Roadmap

* **Cross-ecosystem dependencies:** It is important to trace dependencies across ecosystem boundaries, not just within them. For instance, Pandas [Python] depends on NumPy [Python], which depends on OpenBLAS [C] ([details](https://codeberg.org/vladh/bindep)). This cross-ecosystem view naturally elevates low-level infrastructure libraries in C/C++, Fortran, and similar languages.

* **More value and risk metrics:** We will learn from the first grant distributions and update the model accordingly.

* **More ecosystems:** Go, Java, etc.

