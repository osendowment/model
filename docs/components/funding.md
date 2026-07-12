# Funding (eligibility component)

How well-resourced is a project? The funding component of the
[Eligibility stage](../eligibility.md) gathers every public
signal that a repo receives (or gives) financial support — GitHub Sponsors,
`FUNDING.yml` platforms, the FLOSS Fund directory, npm/PyPI registry funding
fields, OpenCollective budgets, personal Sponsors of the repo's bus-factor
maintainers, FOSS-foundation hosting, and the curated stage overrides — and
distills them into:

1. a **funding-risk score (`score`, 1–100, higher = more at-risk)** stored in
   `data/eligibility/funding.csv` (informational — not carried into any
   aggregate; see below), and
2. two boolean flags — **`intent`** and **`nonprofit`** — that
   `src/eligibility/build_eligibility.py` joins into
   `data/eligibility/eligibility.csv` as two of the four eligibility checks.

Scope: the top repos — valid class-A, **archived included**
(`load_top_repos()`, which includes archived by default), so an archived repo keeps its funding
row and surfaces downstream as `active=False` rather than being dropped
(counts in
the preview stats sheet → Eligibility → Intent and nonprofit;
see [value.md](../value.md)).
Build step: `src/eligibility/build_funding.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[most recent]` = the latest pull of that source; `[2021–2025]` = a 5-year
window. Raw signals are fetched per-source under `data/sources/`; derived
columns are computed by `build_funding.py`.

```
Funding  → data/eligibility/funding.csv  (one row per top repo, archived included)
│
├── GitHub Sponsors
│   ├── gh_sponsors_enabled   ← GraphQL hasSponsorsListing on the repo OWNER (set up to receive)      [most recent]
│   ├── gh_sponsorships_in    ← GraphQL sponsorshipsAsMaintainer (repo OWNER only — FUNDING.yml
│   │                            co-maintainer logins deliberately NOT credited)                       [most recent]
│   ├── gh_sponsorships_out   ← GraphQL sponsorshipsAsSponsor   (repo owner account)                  [most recent]
│   ├── gh_sponsorships       ← derived (in + out, total engagement)                                  [most recent]
│   └── gh_sponsorships_p     ← derived (worst-pinned CDF risk percentile of gh_sponsorships)         [most recent]
│
├── Funding links  (GitHub: GraphQL repository.fundingLinks — the "Sponsor" widget;
│   │               GitLab: gitlab/funding-files.csv — FUNDING.yml variants probed
│   │               on the default branch, same columns)
│   ├── has_funding_links     ← GraphQL fundingLinks (resolves FUNDING.yml anywhere + org default);
│   │                            GitLab: the probed FUNDING.yml parsed to ≥1 platform key             [most recent]
│   ├── has_funding_yml       ← a FUNDING.yml FILE exists for the repo or its owner (even if it
│   │                            resolves to no links — the file itself signals intent); GitLab:
│   │                            FUNDING.yml / .github/FUNDING.yml / .gitlab/FUNDING.yml in the repo  [most recent]
│   └── funding_link_platforms ← declared platform keys (per-platform handles stay in the
│                                source file, github/funding-yml.csv)                                 [most recent]
│
├── FLOSS Fund  (funding.json)
│   └── has_funding_json      ← dir.floss.fund export ∩ repo (id-first join, incl. redirect-resolved
│                                URLs; non-GitHub manifests join by normalized repository URL) OR an
│                                ORG-level manifest (github.com/<org>) covering the owner OR, for a
│                                GitLab repo, an in-repo funding.json / .well-known manifest pointer
│                                (gitlab/funding-files.csv)                                            [most recent]
│
├── Registry funding fields
│   ├── has_npm_funding, npm_funding_url        ← npm package.json `funding` field (npm repos)        [most recent]
│   └── has_pypi_funding, pypi_funding_platforms ← PyPI project_urls funding link (pypi repos)        [most recent]
│
├── OpenCollective
│   ├── oc_slug               ← attributed collective (reverse-map / curated override)                [most recent]
│   ├── oc_avg_funding        ← OC GraphQL totalAmountReceived (gross, mean of years; $0 if none)     [2021–2025]
│   └── oc_avg_funding_p      ← derived (worst-pinned CDF risk percentile of oc_avg_funding)          [2021–2025]
│
├── Bus-factor maintainer Sponsors  (github/maintainer-sponsors.csv ∪ eligibility/maintainer-overrides.csv)
│   └── bf_maintainer_fundable ← any bus-factor maintainer (wrote ≥50% of the repo) has a personal
│                                GitHub Sponsors listing (GraphQL hasSponsorsListing, keyed by user id)  [most recent]
│
├── Institutional backing  (funding/host-by-repo.csv ∪ data/eligibility/overrides.csv
│   │                       ∪ data/eligibility/gitlab-hosts.csv)
│   ├── host, host_type      ← legally-connected steward domain (e.g. apache.org, react.foundation).
│   │                            Precedence: overrides.csv > scraped foundation roster > the curated
│   │                            GitLab-instance mapping — a repo on an institution's OWN GitLab
│   │                            (salsa.debian.org → debian.org, gitlab.gnome.org → gnome, …) is
│   │                            host-backed by that institution; gitlab.com maps to nothing          [most recent]
│   ├── owner, owner_type     ← owning-entity domain (e.g. meta.com), from overrides.csv               [most recent]
│   └── host_score           ← derived: combined backing, most-funded of host/owner (0 · 0.5 · 1)     [most recent]
│
├── paypal                    ← curated PayPal.me URL from overrides.csv (declared channel: feeds intent + channels_count, caps score)  [curated]
├── channels_count           ← derived (funding-link platforms ∪ funding.json channels — repo + org
│                               manifests ∪ npm ∪ pypi ∪ paypal, deduped)                              [most recent]
├── gh_stars, gh_forks        ← GitHub /repos (informational, not scored)                             [most recent]
│
├── score  (funding-risk score)  ← derived (geom-mean of gh_sponsorships_p, oc_avg_funding_p, host_score×100; int 1–100)  [most recent]
│                                  (NOT carried into eligibility.csv or risk.csv — funding is not a scored dimension)
├── intent                     ← derived bool: ≥1 funding signal present (see below) → eligibility.csv
└── nonprofit                  ← derived bool: false only when host_type or owner_type == company → eligibility.csv
```

## How It Works

1. **Collect** — the funding fetchers (FUNDING.yml links, npm/PyPI funding
   fields, Sponsors, bus-factor maintainer Sponsors, FLOSS Fund, the OC
   reverse-map + budgets, and the foundation roster scrapers + host matcher)
   pull raw signals into `data/sources/`, each TTL-controlled so re-runs only
   fetch what's missing or stale.
2. **Join** — `build_funding.py` joins the sources onto the top repos
   (archived included; GitHub-repo signals by the stable `repo_id`, outbound
   sponsoring by owner `login`, OC by `slug`, FLOSS org manifests by owner).
3. **Derive** — combine raw signals (`gh_sponsorships`, `channels_count`,
   `has_funding_json`) and compute the percentiles.
4. **Score** — `score` = geometric mean of the two channel percentiles and
   **`host_score`×100** — the combined backing axis (most-funded of host /
   owner: company 0 · nonprofit 0.5 · none 1); integer 1–100, higher = more at-risk.
5. **Flags** — `build_funding.py` derives the `intent` and `nonprofit` booleans
   from the funding signals (`_intent_flag` / `_nonprofit_flag`) and writes them
   to `funding.csv`; `build_eligibility.py` joins just those two columns into
   `eligibility.csv` alongside the `oss` and `active` checks. The funding
   `score` does **not** feed `eligibility.csv` (or `risk.csv`); funding is a
   signal-only component, not a scored dimension.

Pipeline order (`src/eligibility/run_eligibility_pipeline.py`). The eligibility
runner fetches these sources by default (incremental — each fetcher skips data
already present, so a re-run only fills gaps); pass `--skip-fetch` to rebuild
from existing data without fetching:

```
funding-yml → npm-funding → pypi-funding → sponsors → maintainer-sponsors → floss-fund → oc-collectives → opencollective → foundation scrapers → match-hosts → funding-build
```

(The same runner also fetches the license and EOL signals for the stage's
other dimensions — see [eligibility.md](../eligibility.md).)

## Collection

These sources feed the build. Each fetcher records a `*_status` and/or
`fetched_at`, so a `0`/`False` value is distinguishable from a failed fetch.
Repo-keyed files carry a `repo_id` column — the actual (rename-proof) join key.

| Source file (`data/sources/` unless noted) | Fetcher | Collects | Key |
|---|---|---|---|
| `github/sponsors.csv` | `src/sources/github/fetch_sponsors.py` | inbound GitHub Sponsors count + `gh_sponsors_enabled` | `repo_id` |
| `github/sponsorships.csv` | `src/sources/github/fetch_sponsorships.py` | outbound sponsoring count | `login` |
| `github/maintainer-sponsors.csv` | `src/sources/github/fetch_maintainer_sponsors.py` | personal Sponsors listing per bus-factor maintainer | `user_id` |
| `data/eligibility/maintainer-overrides.csv` | curated | `login,reason` — maintainers fundable via a channel the fetch can't see; unioned into the fundable set | `login` |
| `github/funding-yml.csv` | `src/sources/github/fetch_funding_yml.py` | resolved funding links (GraphQL `fundingLinks`) + FUNDING.yml file presence — platforms + handles | `repo_id` |
| `npm/funding.csv` | `src/sources/npm/fetch_funding.py` | npm package.json `funding` field | `repo_id` |
| `pypi/funding.csv` | `src/sources/pypi/fetch_funding.py` | PyPI `project_urls` funding link | `repo_id` |
| `floss-fund/funding-json.csv` | `src/sources/floss_fund/funding_json.py` | FLOSS Fund manifest directory (repo_id-stamped) | `id` |
| `opencollective/collectives.csv` | `src/sources/opencollective/fetch_collectives.py` | OC ↔ GitHub reverse-map (which repo/org each collective funds) | `slug` |
| `opencollective/budgets.csv` | `src/sources/opencollective/fetch_budgets.py` | OC gross annual budgets | `slug` |
| `funding/host-by-repo.csv` | foundations scrapers (`src/sources/funding/`) | scraped FOSS-foundation host | `repo_id` |
| `data/eligibility/overrides.csv` | curated | per-repo (or `owner/*` org-glob) `host`/`owner` **domains** + types (company/nonprofit), a curated `oc_slug`, and a curated `paypal` PayPal.me URL; funding reads `repo,repo_id,host,host_type,gh_user,owner,owner_type,oc_slug,paypal` — the stage-level `license`/`eol`/`reason` columns are consumed by `build_licenses`/`build_active`, not here | `repo_id` |

`gh_stars` / `gh_forks` are read from `data/sources/github/repos.csv` (GitHub
`/repos`) and carried as informational columns — they are **not** scored.

### GitHub Sponsors — inbound vs outbound

GitHub sponsorship has two directions, and they mean opposite things:

| Metric | GraphQL field | Meaning |
|---|---|---|
| `gh_sponsorships_in` | `sponsorshipsAsMaintainer` | accounts **sponsoring** this repo's owner — owner only: a `github:` login in FUNDING.yml that is not the owner (a co-maintainer) is deliberately NOT credited, since their sponsors fund that person's whole portfolio, not this repo |
| `gh_sponsorships_out` | `sponsorshipsAsSponsor` | accounts the repo's **owner sponsors** (the org's "Sponsoring N") |

`fetch_sponsors` also records **`gh_sponsors_enabled`** — whether the owner has
an active Sponsors listing (set up to receive, even at a public count of 0) —
the intent signal proper. `gh_sponsorships_out` is an account-level property,
so `sponsorships.csv` is keyed by `login` (one row per owner, far fewer than
per-repo) and gap-fills only new owners. A repo whose owner sponsors others
is a *resourced* backer, not an unfunded project — which is why the score uses
**`in + out`** rather than `in − out`.

### Bus-factor maintainer Sponsors (`bf_maintainer_fundable`)

`fetch_sponsors` (above) asks whether a repo's **owner** has Sponsors enabled.
For a repo under a *project org* — `acornjs/acorn`, `serde-rs/json`,
`crossbeam-rs/crossbeam` — the owner is the org, which almost never has a
listing, even though the maintainer who actually wrote the repo does
(marijnh, dtolnay, taiki-e all have personal Sponsors). That intent was
invisible.

`fetch_maintainer_sponsors.py` closes the gap. For every top repo it takes the
**bus-factor set** — the fewest top contributors whose commits cumulatively
reach 50% of the repo (`src/sources/github/bf_contributors.py`) — and checks
whether *any of them personally* has a GitHub Sponsors listing
(GraphQL `hasSponsorsListing`). If so, the repo is `bf_maintainer_fundable` — a
funding-intent signal in its own right (someone who carries this repo can be
funded directly).

Two deliberate choices keep it honest and stable:

- **Bus-factor only, not any contributor.** Restricting to the people who wrote
  ≥50% of the repo means a drive-by contributor who merely happens to have
  Sponsors cannot manufacture intent for a project they did not build.
- **Keyed by numeric user id.** `maintainer-sponsors.csv` is keyed by the
  account's immutable `databaseId` (mirroring the `gh/<n>` repo_id convention),
  so a maintainer who carries several repos (dtolnay → serde/syn/quote…) is
  checked once, and the identity survives a GitHub rename. `login` is stored
  alongside for the join back from the (login-keyed) contributor data and for
  audit; `status` distinguishes a resolved "not fundable" from a failed lookup.

The fetched set is **unioned with a curated override file**,
`data/eligibility/maintainer-overrides.csv` (`login,reason`) — bus-factor
maintainers who solicit funding through a channel the automated fetch can't
see (an external profile funding link, an off-GitHub donation page), so
`hasSponsorsListing` reads False despite real funding intent.

### funding.json from the FLOSS Fund directory

Rather than fetch `funding.json` from every top repo individually,
`funding_json.py` downloads the whole [FLOSS Fund](https://dir.floss.fund)
directory once and `build_funding` matches manifests against the top repos —
by the fetcher-stamped `repo_id` first (rename-proof), with a
canonical-slug fallback for rows the fetcher could not id — with zero
per-repo requests. The export also parses each manifest's
channels into per-platform handles + `channel_platforms`. Two matching refinements
catch manifests a raw URL-equality check would miss:

- **Redirect resolution.** A manifest may point at a redirect rather than a
  GitHub URL (e.g. `tukaani.org/xz/redirect-to-github-xz` →
  `github.com/tukaani-project/xz`). The fetcher follows these and records the
  final GitHub URL in `project_repository_resolved`; `export_repo_slug` prefers
  it over the raw URL. Only non-GitHub URLs are probed (best-effort, cached in
  the export, which refreshes on the shared 365-day funding TTL).
- **Org-level manifests.** A manifest whose repo URL is a GitHub
  *org page* (`github.com/<org>`, not a specific repo) declares funding for the
  whole org. Every in-scope repo under that owner gets `has_funding_json=True`
  (repo- and org-level manifests are folded into the one flag) and inherits the
  org manifest's channels — "if the org is fundable, its repos have
  a funding channel". (GitHub Sponsors and `fundingLinks` are already
  owner/org-inherited per repo, so this only fills the FLOSS-side gap.)

### OpenCollective budgets

Slug discovery unions four sources: `open_collective` handles declared in
`funding-yml.csv`, the FLOSS Fund export (in-scope only), the curated
`oc_slug` overrides, and the **reverse map** (`collectives.csv`, from
`fetch_collectives.py` — OC itself declares which repo/org each collective
funds, catching OC-funded projects that never declared a FUNDING.yml, e.g.
socketio). For each slug `fetch_budgets.py` queries the OC GraphQL API for
`totalAmountReceived` per calendar year 2021–2025 (gross incoming). The API
rate-limits hard unauthenticated (HTTP 429); set `OPENCOLLECTIVE_PERSONAL_TOKEN`
in `.env` to lift it. `oc_avg_funding` is the mean over years with data.

`build_funding` then **attributes** a collective to each repo (`oc_slug`): a
curated override row is authoritative (its `oc_slug` wins even when empty —
an empty slug on a curated row means "no OC", suppressing a spurious
reverse-map match); otherwise a repo-level reverse-map match takes the
collective's full average budget (a real, `oc_status=ok` collective counts
even at $0 raised — the channel itself is intent); otherwise an org-level
match splits the org collective's budget equally across the org's class-A
repos (guarded to non-$0 collectives, since an org-only $0 collective is
usually junk).

## Processing & scoring

### Derived signals

| Column | Formula |
|---|---|
| `gh_sponsorships` | `gh_sponsorships_in + gh_sponsorships_out` |
| `channels_count` | distinct platforms across funding links ∪ funding.json channels (repo + org manifests) ∪ npm ∪ pypi ∪ paypal |
| `has_funding_json` | repo present in the FLOSS Fund export (id-first, incl. redirect-resolved) OR its owner has an org-level manifest (`github.com/<org>`) |
| `bf_maintainer_fundable` | any bus-factor maintainer (≥50%-of-commits set) has a personal GitHub Sponsors listing (∪ curated `maintainer-overrides.csv`) |
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
  nonprofit, so Apache/CNCF/Eclipse/OpenJS/PSF/NumFOCUS/LF, the **GNU project**
  (host `fsf/gnu` — every GNU package is FSF-stewarded), the **FSF** itself,
  **Software Freedom Conservancy** members (incl. Sourceware-hosted libs like
  `libffi`), the **GNOME Foundation** (`gnome/*`, gnome.org), and the **X.Org
  Foundation** (its gitlab.freedesktop.org/xorg projects, e.g. `libxcb`) repos
  drop from 100 → 50. `match_repos` joins each roster to a repo by exact
  `owner/name` slug, a curated foundation org-prefix (`autotools-mirror/*`,
  `gcc-mirror/*`, `coreutils/*` → GNU; `GNOME/*` → GNOME), or apex/suffix domain
  (`*.gnu.org` → GNU, `sfconservancy.org` → SFC, `gnome.org` → GNOME, `x.org` →
  X.Org — but NOT `freedesktop.org`, a broader umbrella that would
  over-attribute). Reference-only subdomains (`peps.python.org`, docs pages) are
  excluded so a repo that merely links to a foundation is not miscredited.
- A curated `data/eligibility/overrides.csv` row sets either side by domain.
  `facebook/watchman` (owner `meta.com` company) → host_score `min(1, 0)` = 0
  → ∛(100·100·0) = **1**; `rust-lang/rust` (host `rustfoundation.org`
  nonprofit, no owner) → host_score `min(0.5, 1)` = 0.5 → ∛(100·100·50) =
  **79**. Overrides evolve with reality: `react/react`
  is stewarded by the React Foundation (nonprofit) with no company owner,
  so it sits at host_score 0.5 → **79** rather
  than the company floor of 1.

Non-backed unfunded repos stay at 100 — **unless they declare a funding channel
whose $ we can't measure**, which caps the score at `DECLARED_FUNDING_CAP` (79).
A project that has set up *a way* to be funded is not maximally unfunded. The cap
fires on: a registry channel (`has_npm_funding` / `has_pypi_funding`), a FLOSS
Fund manifest for the repo or its owner (`has_funding_json`), a curated `paypal`
PayPal.me handle, or a funding
**link** to any platform *other than* GitHub Sponsors / Open Collective. Those two are excluded because their real
dollars already feed the score (`gh_sponsorships_p`, `oc_avg_funding_p`) — a
link to them adds nothing to cap on. So a Liberapay/Ko-fi/Tidelift link caps
(e.g. `tukaani-project/xz` → 79), but a GitHub-Sponsors-only repo with 0
sponsors stays at its measured score.

Worked examples (from the current `funding.csv`):

| Repo | oc_avg | ships | host_score | `score` | Reading |
|---|---:|---:|---:|---:|---|
| facebook/watchman | 0 | 0 | 0 | **1** | company-owned (meta.com) — floors at 1 |
| vuejs/core | $132,860 | 0 | 1 | 13 | OC-funded, no institutional backer |
| axios/axios | $32,031 | 20 | 1 | 16 | funded both channels, no backer |
| zloirock/core-js | $34,530 | 0 | 1 | 26 | OC-funded only |
| rust-lang/rust | 0 | 0 | 0.5 | 79 | nonprofit foundation host only |
| acornjs/acorn | 0 | 0 | 1 | **100** | no funding, no backer |

Note the third axis lifts *funded-but-unbacked* repos (vuejs, axios sit well
above the two-axis geometric mean of their channel percentiles): "no
institutional backer" (`host_score = 1` → backing 100) is a
risk voice, not a no-op. (Intent/nonprofit coverage → the preview stats sheet.)

## Output

### `data/eligibility/funding.csv` (per-dimension build)

One row per top repo (archived included). No `fetched_at` — per-signal
timestamps stay in each source file.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `gh_sponsors_enabled` | the owner has an active GitHub Sponsors listing (set up to receive) |
| `gh_sponsorships_in` | inbound GitHub Sponsors count (owner only) |
| `gh_sponsorships_out` | outbound sponsoring count (owner) |
| `gh_sponsorships` | `in + out` |
| `gh_sponsorships_p` | risk percentile of `gh_sponsorships` |
| `gh_stars`, `gh_forks` | GitHub stars / forks (informational, not scored) |
| `has_funding_links` | repo declares ≥1 funding link (GitHub's resolved `fundingLinks`) |
| `has_funding_yml` | a FUNDING.yml file exists for the repo or its owner (even if it resolves to no links) |
| `funding_link_platforms` | declared platform keys (comma-sep) |
| `has_funding_json` | repo (or its owner, via an org-level manifest) registered in the FLOSS Fund directory (incl. redirect-resolved URL) — a declared channel (caps `score` at 79) |
| `has_npm_funding`, `npm_funding_url` | npm package.json `funding` field — a declared channel (caps `score` at 79) |
| `has_pypi_funding`, `pypi_funding_platforms` | PyPI `project_urls` funding link — a declared channel (caps `score` at 79) |
| `paypal` | curated PayPal.me URL from `overrides.csv` — a declared channel (feeds `intent` + `channels_count`, caps `score` at 79); empty when none |
| `bf_maintainer_fundable` | a bus-factor maintainer (≥50%-of-commits set) has a personal GitHub Sponsors listing (∪ curated overrides); intent-only — not a channel, no cap |
| `channels_count` | distinct funding platforms (links ∪ funding.json repo+org channels ∪ npm ∪ pypi ∪ paypal) |
| `oc_slug` | attributed Open Collective slug (override / reverse-map), empty when none |
| `oc_avg_funding` | mean OC gross annual budget (`0` if none) |
| `oc_avg_funding_p` | risk percentile of `oc_avg_funding` |
| `host` | legally-connected steward **domain** (e.g. `apache.org`, `react.foundation`) or empty |
| `host_type` | `company` / `nonprofit` / empty |
| `owner` | owning-entity **domain** (e.g. `meta.com`), from `overrides.csv` |
| `owner_type` | `company` / `nonprofit` / empty |
| `host_score` | combined backing = `min(type(host), type(owner))` ∈ {`0` company, `0.5` nonprofit, `1` none} — the third score axis (×100) |
| `score` | **funding-risk score** (geom-mean of the two `_p` and `host_score`×100, int 1–100) |
| `intent`, `nonprofit` | the two boolean eligibility flags (see below) |

### `data/eligibility/eligibility.csv` (rollup)

The funding `score` does **not** contribute to any aggregate — it feeds
neither the eligibility rollup nor the `risk.csv` `score` (the geometric mean
of the four scored risk dimensions: `concentration`, `complexity`, `security`,
`workload`).

Instead, `src/eligibility/build_eligibility.py` joins the two boolean flag
columns from `funding.csv` into `data/eligibility/eligibility.csv`, where they
are two of the four checks behind
`eligible = oss AND intent AND nonprofit AND active`
(see [eligibility.md](../eligibility.md)):

| `eligibility.csv` column | Source | Default |
|---|---|---|
| `intent` | `true` when the repo has ≥1 funding signal (see below) | `false` |
| `nonprofit` | `false` only when `host_type`/`owner_type == company` | `true` |

### `intent` and `nonprofit` flags

**`intent`** (`bool`, default `false`) — `true` when the repo shows at least
one funding signal: the owner's GitHub Sponsors listing being enabled
(`gh_sponsors_enabled` — any inbound count implies it; **outbound sponsoring is
NOT intent**, funding others is not a channel for this repo), a resolved
funding link (`has_funding_links`), a FUNDING.yml file present for the repo or
owner even when it resolves to no links (`has_funding_yml`),
a `funding.json` (FLOSS Fund, repo- or org-level), an npm `funding` field, a PyPI project-URLs
funding entry, a bus-factor maintainer who is personally fundable on GitHub
Sponsors (`bf_maintainer_fundable`), an Open Collective slug, a curated `paypal`
PayPal.me handle, or an institutional host or owner. "Intent" means the project
is actively seeking or accepting support.

`intent` then **propagates at the owner level** (`_propagate_owner_intent`): once
ANY repo an owner has in scope declares a *self-declared* channel (Sponsors
enabled, funding links or the FUNDING.yml file, a FLOSS manifest, an npm/PyPI
funding field, or an Open Collective — not the institutional
host/owner backing), every repo the owner has is set `intent=true`. This mirrors
GitHub's own semantics — an org's `.github/FUNDING.yml` and a personal account's
Sponsors listing already apply to all of an owner's repos — so a repo that merely
omits its own `FUNDING.yml` (e.g. `serde-rs/json` while sibling `serde-rs/serde`
declares `github: dtolnay`) is still counted fundable. Propagation only *adds*
intent; `nonprofit` is untouched, so a company-owned org stays ineligible.

**`nonprofit`** (`bool`, default `true`) — `false` only when a corporate entity
(Meta, Google, Microsoft, AWS, …) is the project's host or owner, as determined by
`host_type` / `owner_type == "company"` in `data/eligibility/funding.csv`.
Community-run and foundation-backed projects are `nonprofit=true`.
Corporate-backed repos remain in `eligibility.csv` flagged `nonprofit=false` —
already-resourced projects become ineligible without being hidden.

## Coverage

See the preview stats sheet → Eligibility → Intent and nonprofit
for current per-channel coverage over the top repos and the score distribution.

## Limitations

- **Still GitHub/OC-centric for $ signal.** The two *scored axes* are inbound/
  outbound GitHub Sponsors and OpenCollective. **Institutional backing is now
  folded in** (the `host_score` factor — scraped foundations plus curated
  legally-connected company/foundation overrides), but a repo with no `host`/`owner`
  override and no scraped foundation host still shows little off-platform signal
  (corporate payroll, private grants, VC) — except via
  the outbound-sponsoring proxy. `astral-sh/ruff` was the canonical case —
  VC-backed yet `gh_sponsorships_in = 0` — until a curated owner override
  (`astral.sh`, company) pinned it to `score` 1 / `nonprofit=false`; corporate
  backing that nobody has curated yet still goes unseen.
- **funding.json is still negligible** in this cohort (coverage →
  the preview stats sheet) — the structured-manifest
  ecosystem hasn't reached it. `has_funding_json` does feed `intent` and the
  declared-channel score cap, but carries no dollar signal.
- **`score` is a percentile, not a class.** It's a 1–100 risk number, not a
  class tier. It lives in `funding.csv` and feeds no aggregate — the risk
  `score` uses its four scored dimensions only (concentration, complexity,
  security, workload), and the eligibility rollup consumes just the
  `intent`/`nonprofit` booleans.
- **OC is the only $ amount.** GitHub Sponsors and Patreon/Tidelift amounts
  aren't public, so dollar figures exist only for the repos with an attributed
  Open Collective (coverage → the preview stats sheet);
  the sponsorship axis is a *count*, not a *sum*.
