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

All recipient projects will be under an [OSI-approved license](https://opensource.org/licenses).

### Ecosystems

We aim to focus our support on the core of open-source ecosystems — like ~1% of packages accounting for 99% of downloads and dependencies. Our model shall be a data-driven approximation of the global usage of the open-source supply chain, helping to detect its most critical but underfunded elements.

For us, it is important to drill down dependencies from popular ecosystems like Python and JS/TS to lower-level ecosystems. For instance, Pandas [Python] depends on NumPy [Python], which depends on OpenBLAS [C] ([details](https://codeberg.org/vladh/bindep)). This approach should eventually create a natural priority for low-level infra libraries in C/C++, Fortran, etc.﻿

### Model Development

Besides deciding how to divide grants between ecosystems, we have to prioritize OSS projects within each ecosystem. Our goal is to make this process clear and easy to measure, using both data and human-in-the-loop elements at the start. The model is currently under development, and its ready-to-use version will come from discussions with OSE donors.

Our approach will likely combine Value and Risk scores ([example](https://kvinogradov.com/algo-sponsors/)). Here are some possible parts that might be included: a (+) means the metric increases along with the component (if everything else stays the same), and a (-) means the opposite. These components are just for illustration purposes, and our grantmaking process will use a more comprehensive model for prioritization.﻿

**Value for the Ecosystem** 

* Usage
  * (+) # dependents, based on data from package managers, GitHub, OSE analysis, etc.
  * (+) # downloads: based on data from package managers, OSE analysis, etc.
* Manual Highlights
  * (+) Qualified funding requests
  * (+) Endorsements from OSE donors
  * (+) Open community nominations

#### Risk of the Project

* Complexity & Security
  * (+) LOCs
  * (+) [OpenSSF score](https://scorecard.dev) 
* Maintainance 
  * (–) Active developers 
  * (–) Bus factor ([example](https://github.com/JetBrains-Research/bus-factor-explorer))
  * (+) Issued submitted
* Funding 
  * (–) # GitHub Sponsors
  * (–) Known existing funding
  * (+) Funding requests
