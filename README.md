# Open Source Endowment Fund Distribution Model

This is the work-in-progress fund distribution model for the [Open Source Endowment][ose].

Work is currently happening in this repo and the following places:

* [bindep][bindep] ([@vladh][vlad.website]) — Strategies for finding binary dependencies
* [software-finder][software-finder] ([@jring-o][jring-o]) — PyPI to GitHub repository mapper

[bindep]: https://codeberg.org/vladh/bindep
[jring-o]: https://github.com/jring-o/software-finder
[ose]: https://endowment.dev
[software-finder]: https://github.com/jring-o/software-finder
[vlad.website]: https://vlad.website


## High-level Overview

### Principles

1. We aim to build a transparent, measurable, and verifiable model that can be iteratively improved by the open-source community and approved by a majority of active OSE donors.

2. It will never be a perfect model, because (1) open-source consumption cannot be measured with 100% precision, and (2) there is no ideal consensus on how to prioritize OSS grants based on measurable inputs alone.

### Ecosystems

We aim to focus our support on the core of open-source ecosystems — roughly the ~1% of packages that account for 99% of downloads and dependencies. Our model is a data-driven approximation of global open-source supply chain usage, designed to surface its most critical yet underfunded components.

It is important to trace dependencies across ecosystem boundaries, not just within them. For instance, Pandas [Python] depends on NumPy [Python], which depends on OpenBLAS [C] ([details](https://codeberg.org/vladh/bindep)). This cross-ecosystem view naturally elevates low-level infrastructure libraries in C/C++, Fortran, and similar languages.

### Model Development

Beyond dividing grants between ecosystems, we need to prioritize individual OSS projects within each one. Our goal is to make this process transparent and quantifiable, combining automated scoring with human judgment, especially in the early stages. The model is under active development; its final form will emerge from discussions with OSE donors.

Our approach will likely combine Value and Risk scores ([example](https://kvinogradov.com/algo-sponsors/)). The components below are illustrative: (+) means the metric increases with the component, (–) means it decreases.

**Value for the Ecosystem**

* Usage
  * (+) Number of dependents, based on data from package managers, GitHub, OSE analysis, etc.
  * (+) Number of downloads, based on data from package managers, OSE analysis, etc.
* Manual highlights
  * (+) Qualified funding requests
  * (+) Endorsements from OSE donors

**Risk of the Project**

* Complexity & security
  * (+) Lines of code
  * (+) [OpenSSF score](https://scorecard.dev)
* Maintenance
  * (–) Active developers
  * (–) Bus factor ([example](https://github.com/JetBrains-Research/bus-factor-explorer))
  * (+) Issues submitted
* Funding
  * (–) GitHub Sponsors and other known funding
  * (+) Funding requests
