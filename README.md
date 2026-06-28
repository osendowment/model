# Open Source Endowment Fund Distribution Model

This is the work-in-progress fund distribution model for the [Open Source Endowment][ose].

## High-level Overview

### Principles

1. We aim to build a transparent, measurable, and verifiable model that can be iteratively improved by the open-source community and approved by a majority of active OSE donors.

2. It will never be a perfect model, because (1) open-source consumption cannot be measured with 100% precision, and (2) there is no ideal consensus on how to prioritize OSS grants.

### Ecosystems

We aim to focus our support on the core of open-source ecosystems — roughly the ~1% of packages that account for 99% of downloads and dependencies. Our model is a data-driven approximation of global open-source supply chain usage, designed to surface its most critical yet underfunded components.

It is important to trace dependencies across ecosystem boundaries, not just within them. For instance, Pandas [Python] depends on NumPy [Python], which depends on OpenBLAS [C] ([details](https://codeberg.org/vladh/bindep)). This cross-ecosystem view naturally elevates low-level infrastructure libraries in C/C++, Fortran, and similar languages.

### Model Development

Beyond dividing grants between ecosystems, we need to prioritize individual OSS projects within each one. Our goal is to make this process transparent and quantifiable, combining automated scoring with human judgment, especially in the early stages. The model is under active development; its final form will emerge from discussions with OSE donors.

Our approach is a two-stage automated pipeline — **Value → Risk** — followed by a manual **Eligibility** review of the top candidates. Each step narrows the set the next operates on:

| Step | Goal | Implemented | Roadmap |
|------|------|-------------|---------|
| **[Value](docs/value.md)** | Find most important packages in ecosystems | Download-weighted PageRank for Python (PyPI), Rust (crates), JS/TS (npm), C/C++ (Debian, Homebrew) based on dependency trees, covering 95% downloads in each ecosystem | Community nominations, critical software lists, cross-ecosystem dependencies |
| **[Risk](docs/risk.md)** | Prioritize risky projects among most valuable | Bus factor and Herfindahl--Hirschman index for contributors, complexity metrics (LOC, etc) using [scc](https://github.com/boyter/scc) | [OpenSSF scorecard](https://scorecard.dev), active maintainers, issue activity, GitHub Sponsors |
| **Eligibility** *(manual)* | Filter to fundable projects | Checked manually for the top candidates — OSS license, EOL, and independence (no corporate trademarks, no associated startups, community-led) | Automate the license / EOL / independence checks |

Detailed methodology lives in [`docs/`](docs/): one page per automated stage — [value](docs/value.md), [risk](docs/risk.md) — plus [`docs/sources/`](docs/sources/) (one page per data source) and [`docs/components/`](docs/components/) (cross-cutting components — the per-language value pipelines [python](docs/components/python.md) / [javascript](docs/components/javascript.md) / [cpp](docs/components/cpp.md) / [rust](docs/components/rust.md), plus the [funding](docs/components/funding.md) and [validation](docs/components/validation.md) tables). The code mirrors this: `src/sources/<source>/` for fetch/process scripts, `src/{value,risk}/` for the stage pipelines, and `src/common/` for shared infrastructure.

Work is currently happening in this repo and the following places:

* [bindep][bindep] ([@vladh][vlad.website]) — Strategies for finding binary dependencies
* [software-finder][software-finder] ([@jring-o][jring-o]) — PyPI to GitHub repository mapper

[bindep]: https://codeberg.org/vladh/bindep
[jring-o]: https://github.com/jring-o/software-finder
[ose]: https://endowment.dev
[software-finder]: https://github.com/jring-o/software-finder
[vlad.website]: https://vlad.website
