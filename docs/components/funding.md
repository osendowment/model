# Funding (eligibility component)

How well-resourced is a project? This component of the
[Eligibility stage](../eligibility.md) collects every public signal that a repo
receives or gives financial support, then emits:

1. a **funding-risk score** (`score`, 0–100, higher = more at-risk) in
   `data/eligibility/funding.csv` — informational, fed into no aggregate, and
2. two booleans — **`intent`** and **`nonprofit`** — which
   `src/eligibility/build_eligibility.py` joins into
   `data/eligibility/eligibility.csv` as two of the four eligibility checks.

Scope: the top repos — valid class-A, **archived included** (`load_top_repos()`
keeps archived rows by default). An archived repo keeps its funding row and
surfaces downstream as `active=False`.
Build step: `src/eligibility/build_funding.py`.

## Metrics roadmap

Each leaf is one column with its source and period. `[most recent]` = the latest
pull of that source; `[2021–2025]` = a 5-year window. Column meanings are in
[Output](#dataeligibilityfundingcsv); the mechanisms are in the sections below.

```
Funding  → data/eligibility/funding.csv  (one row per top repo, archived included)
│
├── GitHub Sponsors            ← github/sponsors.csv + github/sponsorships.csv
│   ├── gh_sponsors_enabled    ← GraphQL hasSponsorsListing on the repo OWNER          [most recent]
│   ├── gh_sponsorships_in     ← GraphQL sponsorshipsAsMaintainer (OWNER only)         [most recent]
│   ├── gh_sponsorships_out    ← GraphQL sponsorshipsAsSponsor (owner account)         [most recent]
│   ├── gh_sponsorships        ← derived: in + out                                     [most recent]
│   └── gh_sponsorships_p      ← derived: worst-pinned CDF risk percentile             [most recent]
│
├── Funding links              ← github/funding-yml.csv ∪ gitlab/funding-files.csv
│   ├── has_funding_links      ← ≥1 resolved funding link                              [most recent]
│   ├── has_funding_yml        ← a FUNDING.yml file exists for the repo or its owner   [most recent]
│   └── funding_link_platforms ← declared platform keys (handles stay in the source)   [most recent]
│
├── FLOSS Fund
│   └── has_funding_json       ← floss-fund/funding-json.csv, repo- or org-level       [most recent]
│
├── Registry funding fields
│   ├── has_npm_funding, npm_funding_url         ← npm/funding.csv                     [most recent]
│   └── has_pypi_funding, pypi_funding_platforms ← pypi/funding.csv                    [most recent]
│
├── OpenCollective             ← opencollective/collectives.csv + budgets.csv
│   ├── oc_slug                ← attributed collective (override / reverse map)        [most recent]
│   ├── oc_avg_funding         ← OC totalAmountReceived, gross, mean of years            [2021–2025]
│   └── oc_avg_funding_p       ← derived: worst-pinned CDF risk percentile               [2021–2025]
│
├── Bus factor maintainer Sponsors ← github/maintainer-sponsors.csv ∪ eligibility/maintainer-overrides.csv
│   └── bf_maintainer_fundable ← a ≥50%-of-commits maintainer has a personal listing   [most recent]
│
├── Institutional backing      ← funding/host-by-repo.csv ∪ eligibility/overrides.csv
│   │                            ∪ eligibility/gitlab-hosts.csv
│   ├── host, host_type        ← legally-connected steward; roster code or domain      [most recent]
│   ├── owner, owner_type      ← owning-entity domain, from overrides.csv only         [most recent]
│   └── host_score             ← derived: min(type(host), type(owner)) ∈ {0, 0.5, 1}   [most recent]
│
├── paypal                     ← curated PayPal.me URL from overrides.csv                  [curated]
├── channels_count             ← derived: distinct declared platforms, deduped         [most recent]
├── gh_stars, gh_forks         ← github/repos.csv (informational, not scored)          [most recent]
│
├── score                      ← derived: ∛(gh_sponsorships_p × oc_avg_funding_p × host_score×100)
├── intent                     ← derived bool → eligibility.csv
└── nonprofit                  ← derived bool → eligibility.csv
```

The TTL-controlled fetchers write raw signals into `data/sources/`.
`build_funding.py` joins them onto the top repos by `repo_id` (GitHub repo
signals), owner `login` (outbound sponsoring, FLOSS org manifests) or `slug`
(OC), derives the columns marked *derived* above, and writes `funding.csv`.

## Pipeline order

Run via `scripts/run-pipeline.sh --stage eligibility`
(`src/eligibility/run_eligibility_pipeline.py`). Fetchers are incremental; pass
`--refresh` to force a refetch. Steps in `[…]` share a parallel
group; everything else is serial.

```
bf-contributors
  → [funding-yml · sponsorships · gitlab-funding · npm-funding · pypi-funding
     · sponsors · maintainer-sponsors · floss-fund · oc-collectives]
  → opencollective
  → [apache · cncf · eclipse · fsf · gnome · gnu · lf · xorg · numfocus · openjs · psf · sfc]
  → match-hosts
  → funding-build
```

`bf-contributors` runs serially and first: both `maintainer-sponsors` and
`funding-build` read its output. `opencollective` (budgets) stays serial after
the group because it reads `oc-collectives`.

The same runner also fetches the license and EOL signals for the stage's other
dimensions — see [eligibility.md](../eligibility.md).

## Collection

Each fetcher records a `*_status` and/or `fetched_at`, so a `0`/`False` value
stays distinguishable from a failed fetch. Repo-keyed files carry `repo_id` —
the actual join key.

| Source file (`data/sources/` unless noted) | Fetcher | Collects | Key |
|---|---|---|---|
| `github/sponsors.csv` | `src/sources/github/fetch_sponsors.py` | inbound GitHub Sponsors count + `gh_sponsors_enabled` | `repo_id` |
| `github/sponsorships.csv` | `src/sources/github/fetch_sponsorships.py` | outbound sponsoring count | `login` |
| `github/maintainer-sponsors.csv` | `src/sources/github/fetch_maintainer_sponsors.py` | personal Sponsors listing per bus factor maintainer | `user_id` |
| `github/contributor-commits.csv` | `src/sources/github/fetch_contributors_metrics.py` — **`bf-contributors` step, 365-day TTL** | GitHub `/contributors` rows; the bus factor membership behind `bf_maintainer_fundable` | `repo_id` |
| `data/eligibility/maintainer-overrides.csv` | curated | `login,reason` — maintainers fundable through a channel the fetch cannot see | `login` |
| `github/funding-yml.csv` | `src/sources/github/fetch_funding_yml.py` | resolved funding links (GraphQL `fundingLinks`) + FUNDING.yml file presence — platforms + handles | `repo_id` |
| `gitlab/funding-files.csv` | `src/sources/gitlab/fetch_funding_files.py` | the GitLab twin of `funding-yml.csv` — FUNDING.yml variants + an in-repo `funding.json` / `.well-known` pointer, probed on the default branch | `repo_id` |
| `npm/funding.csv` | `src/sources/npm/fetch_funding.py` | npm package.json `funding` field | `repo_id` |
| `pypi/funding.csv` | `src/sources/pypi/fetch_funding.py` | PyPI `project_urls` funding link | `repo_id` |
| `floss-fund/funding-json.csv` | `src/sources/floss_fund/funding_json.py` | FLOSS Fund manifest directory (repo_id-stamped) | `id` |
| `opencollective/collectives.csv` | `src/sources/opencollective/fetch_collectives.py` | OC ↔ GitHub reverse map (which repo/org each collective funds) | `slug` |
| `opencollective/budgets.csv` | `src/sources/opencollective/fetch_budgets.py` | OC gross annual budgets + `oc_status` | `slug` |
| `funding/host-by-repo.csv` | foundation scrapers (`src/sources/funding/`) → `match_repos` | scraped FOSS-foundation host, as a roster code | `repo_id` |
| `data/eligibility/gitlab-hosts.csv` | curated | `gitlab_host,host,host_type,reason` — GitLab instance → institutional host (`salsa.debian.org` → `debian.org`). `gitlab.com` is deliberately absent | `gitlab_host` |
| `data/eligibility/overrides.csv` | curated | per-repo (or `owner/*` org-glob) backing. `build_funding` reads `repo, repo_id, host, host_type, owner, owner_type, oc_slug, paypal`; the `license`/`eol`/`reason`/`gh_user` columns belong to other builders or to audit | `repo_id` |

`gh_stars` / `gh_forks` come from `data/sources/github/repos.csv` and are
informational — never scored.

### GitHub Sponsors — inbound vs outbound

| Metric | GraphQL field | Meaning |
|---|---|---|
| `gh_sponsorships_in` | `sponsorshipsAsMaintainer` | accounts **sponsoring** this repo's owner |
| `gh_sponsorships_out` | `sponsorshipsAsSponsor` | accounts the repo's **owner sponsors** |

Inbound counts the owner only. A `github:` login in FUNDING.yml that is not the
owner is a co-maintainer, whose sponsors fund that person's whole portfolio, so
the fetch does not credit them.

`fetch_sponsors` also records **`gh_sponsors_enabled`** — the owner has an
active Sponsors listing, even at a public count of 0. That is the intent signal
proper. `gh_sponsorships_out` is an account-level property, so
`sponsorships.csv` is keyed by `login` and gap-fills only new owners. An owner
who sponsors others is a *resourced* backer, so the score uses **`in + out`**,
not `in − out`.

### Bus factor maintainer Sponsors (`bf_maintainer_fundable`)

`fetch_sponsors` covers the repo's **owner**. Under a project org —
`acornjs/acorn`, `serde-rs/json`, `crossbeam-rs/crossbeam` — the owner is the
org, which rarely has a listing, even though the maintainer who wrote the repo
does (marijnh, dtolnay, taiki-e).

`fetch_maintainer_sponsors.py` closes that gap. For each top repo it takes the
**bus factor set** — the fewest top contributors whose commits cumulatively
reach 50% of the repo (`src/sources/github/bf_contributors.py`) — and checks
whether any of them personally has a GitHub Sponsors listing (GraphQL
`hasSponsorsListing`).

- **Bus factor only, not any contributor.** A drive-by contributor with Sponsors
  cannot manufacture intent for a project they did not build.
- **Keyed by numeric user id.** `maintainer-sponsors.csv` keys on the account's
  immutable `databaseId`, so a maintainer who carries several repos
  (dtolnay → serde/syn/quote) is checked once and survives a rename. `login` is
  stored alongside for the join back and for audit; `status` separates a
  resolved "not fundable" from a failed lookup.
- **Unioned with a curated override.** `maintainer-overrides.csv`
  (`login,reason`) lists maintainers who solicit funding through a channel the
  fetch cannot see, so `hasSponsorsListing` reads False despite real intent.

The bus factor set comes from `data/sources/github/contributor-commits.csv`,
written by the `bf-contributors` step (365-day per-row TTL). This is a
**different file** from the git-clone contributor log
`data/sources/git/contributor-commits.csv`, which the risk stage fetches
independently for the concentration score.

### funding.json from the FLOSS Fund directory

`funding_json.py` downloads the whole [FLOSS Fund](https://dir.floss.fund)
directory once, instead of probing every top repo. `build_funding` matches a
manifest by the fetcher-stamped `repo_id` first (rename-proof), then by
canonical slug, then by normalized repository URL — zero per-repo requests. The
export also parses each manifest's channels into per-platform handles plus
`channel_platforms`.

Two refinements catch manifests a raw URL-equality check would miss:

- **Redirect resolution.** A manifest may point at a redirect rather than a
  GitHub URL (`tukaani.org/xz/redirect-to-github-xz` →
  `github.com/tukaani-project/xz`). The fetcher follows it and records the final
  URL in `project_repository_resolved`; `export_repo_slug` prefers that over the
  raw URL. Only non-GitHub URLs are probed. Results cache in the export, which
  refreshes on the shared 365-day funding TTL.
- **Org-level manifests.** A manifest whose repo URL is a GitHub *org page*
  (`github.com/<org>`) declares funding for the whole org. Every in-scope repo
  under that owner gets `has_funding_json=True` and inherits the org manifest's
  channels. GitHub Sponsors and `fundingLinks` are already owner-inherited, so
  this only fills the FLOSS-side gap.

### OpenCollective budgets

Slug discovery unions four sources: `open_collective` handles in
`funding-yml.csv`, the FLOSS Fund export (in-scope only), the curated `oc_slug`
overrides, and the **reverse map** (`collectives.csv` — OC itself declares which
repo or org each collective funds, catching OC-funded projects that never wrote
a FUNDING.yml, e.g. socketio).

`fetch_budgets.py` queries the OC GraphQL API for `totalAmountReceived` per
calendar year 2021–2025 (gross incoming). The API rate-limits hard
unauthenticated (HTTP 429); set `OPENCOLLECTIVE_PERSONAL_TOKEN` in `.env` to
lift it. `oc_avg_funding` is the mean over years with data.

#### Attributing a collective to a repo

`build_funding` picks `oc_slug` by first match:

| # | Rule | Amount |
|---|---|---|
| 1 | a curated `overrides.csv` row for this `repo_id` | its `oc_slug`, or `""` when the cell is empty |
| 2 | repo-level reverse-map match on the repo slug | the collective's full average budget |
| 3 | repo-level match on the normalized clone URL (GitLab repos) | the collective's full average budget |
| 4 | org-level match, repo is class-A | the org collective's budget ÷ the org's class-A repo count |
| 5 | no match | `""`, `0` |

Rules 2–4 share one guard: `_real_oc(slug)` — the collective was fetched
successfully and carries `oc_status == ok`. There is **no dollar threshold at
any level.** A real collective that raised $0 still attributes, because the
channel itself is the intent signal, and `oc_avg_funding` carries the $0
separately. The consequence is visible in `funding.csv`: an org-level
collective at $0 (`for-the-mage` claiming `github.com/facebook`,
`nodejs-google-summer-of-code`) attaches to that org's class-A repos.

Rule 1 is the fix for a wrong match. A curated row is authoritative **even with
an empty `oc_slug`** — an empty cell on a curated row means "no OC" and
suppresses a spurious reverse-map match.

## Processing and scoring

### Derived signals

| Column | Formula |
|---|---|
| `gh_sponsorships` | `gh_sponsorships_in + gh_sponsorships_out` |
| `channels_count` | distinct platforms across funding links ∪ funding.json channels (repo + org) ∪ npm ∪ pypi ∪ paypal |
| `has_funding_json` | repo present in the FLOSS Fund export (id-first, incl. redirect-resolved) OR its owner has an org-level manifest |
| `bf_maintainer_fundable` | any bus factor maintainer has a personal GitHub Sponsors listing (∪ curated `maintainer-overrides.csv`) |
| `oc_avg_funding` | mean of the OC `raised_*` years (`0` when no OC presence) |

### The score

Each channel becomes a **worst-pinned CDF risk percentile** — lower funding
ranks higher (more at-risk), mirroring the negated `openssf_score` in the
security component. Both percentiles are computed over all the top repos
(`gh_sponsorships` defaults to 0, `oc_avg_funding` to $0).

```
score      = max(1, round( ∛(gh_sponsorships_p × oc_avg_funding_p × host_score×100) ))
host_score = min( type(host), type(owner) )      # TYPE_SCORE: company 0 · nonprofit 0.5 · none 1
```

`host_score` enters scaled ×100 (company 0 · nonprofit 50 · none 100) so it is
commensurate with the two percentiles — one of three equal voices, not a blunt
multiplier.

The **geometric mean** is the key choice: a repo funded strongly on *either*
channel gets a low (good) score, because one low percentile pulls the product
down. A project with no GitHub Sponsors but a healthy OpenCollective reads as
funded.

### `host_score` — institutional backing

`host` is the foundation or company **legally** stewarding the project; a loose
community association does not count. `owner` is the owning entity, always a
domain from `overrides.csv`. Each is typed `company` (0 — fully resourced,
score floors at 1), `nonprofit` (0.5 — halved), or empty (1 — unchanged).
`host_score` takes the most-funded of the two (`min`).

`host` values come from three origins, in precedence order:

| Origin | Value shape | Example |
|---|---|---|
| `data/eligibility/overrides.csv` (curated) | domain | `rustfoundation.org`, `react.foundation`, `sqlite.org` |
| `funding/host-by-repo.csv` (scraped rosters, always nonprofit) | roster **code** | `apache`, `psf`, `lf`, `lf/cncf`, `lf/openjs`, `fsf/gnu`, `numfocus`, `sfc`, `gnome`, `xorg` |
| `data/eligibility/gitlab-hosts.csv` (curated) | domain | `debian.org`, `freedesktop.org`, `kde.org`, `inria.fr` |

A roster host is nonprofit by definition — `build_funding` hard-codes
`host_type = "nonprofit"` for a scraped match — so a roster-matched repo with
no company `owner` drops from backing 100 → 50. `fsf/gnu` covers every GNU
package (the FSF holds the copyright assignments); `sfc` covers Software
Freedom Conservancy members, including Sourceware-hosted libs like `libffi`.
A curated `overrides.csv` `host_type` outranks it.

`match_repos` joins a roster to a repo three ways: exact `owner/name` slug; a
curated foundation org prefix (`autotools-mirror/*`, `gcc-mirror/*`,
`coreutils/*` → GNU; `GNOME/*` → GNOME); or an apex/suffix homepage domain via
`DOMAIN_SUFFIX_HOST` (`*.gnu.org` → GNU, `sfconservancy.org` → SFC,
`gnome.org` → GNOME, `x.org` → X.Org). Reference-only subdomains
(`peps.python.org`, docs pages) are excluded, so a repo that merely links to a
foundation is not miscredited.

`DOMAIN_SUFFIX_HOST` deliberately omits `freedesktop.org`: as a *homepage
domain* it is a broad umbrella (wayland, dbus and pipewire are not X.Org
projects) and would over-attribute. Hosting **on** `gitlab.freedesktop.org` is
different evidence — the instance is fd.o's own — so `gitlab-hosts.csv` maps it
to `host=freedesktop.org, nonprofit`. The two rules do not conflict: one reads
a declared homepage, the other reads the repo's actual host.

### The declared-channel cap

An unbacked, unfunded repo scores 100 — **unless it declares a funding channel
whose dollars we cannot measure**, which caps the score at
`DECLARED_FUNDING_CAP` (79). A project that has set up *a way* to be funded is
not maximally unfunded.

`_declares_unmeasured_channel` fires on: `has_npm_funding`, `has_pypi_funding`,
`has_funding_json` (repo or owner), a curated `paypal` handle, or a funding
link to any platform outside `MEASURED_PLATFORMS = {github, open_collective}`.
Those two are excluded because their real dollars already feed the score. So a
Liberapay / Ko-fi / Tidelift link caps, but a GitHub-Sponsors-only repo with 0
sponsors keeps its measured score.

`bf_maintainer_fundable` is intent-only: it is not a channel and never caps,
because a personal Sponsors listing funds the maintainer's whole portfolio.

### Worked examples

Illustrative inputs, not measurements — they show how the three axes combine.

| `gh_sponsorships_p` | `oc_avg_funding_p` | `host_score` | `score` | Reading |
|---:|---:|---:|---:|---|
| 100 | 100 | 0 (company owner) | **1** | company-owned — floors at 1 |
| 100 | 100 | 0.5 (nonprofit host) | 79 | foundation-hosted, no measured dollars |
| 100 | 100 | 1 (none) | **100** | no funding, no backer |
| 100 | 20 | 1 (none) | 58 | OC-funded, no institutional backer |
| 60 | 20 | 1 (none) | 49 | funded on both channels, no backer |
| 100 | 100 | 1 (none) + Liberapay link | 79 | unmeasured declared channel caps at 79 |

The third axis lifts funded-but-unbacked repos above the two-axis mean of their
channel percentiles: "no institutional backer" (`host_score = 1` → backing 100)
is a risk voice, not a no-op.

## Output

### `data/eligibility/funding.csv`

One row per top repo, archived included. No `fetched_at` — per-signal
timestamps stay in each source file.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `gh_sponsors_enabled` | the owner has an active GitHub Sponsors listing |
| `gh_sponsorships_in` | inbound GitHub Sponsors count (owner only) |
| `gh_sponsorships_out` | outbound sponsoring count (owner) |
| `gh_sponsorships` | `in + out` |
| `gh_sponsorships_p` | risk percentile of `gh_sponsorships` |
| `gh_stars`, `gh_forks` | GitHub stars / forks (informational, not scored) |
| `has_funding_links` | repo declares ≥1 funding link (GitHub's resolved `fundingLinks`) |
| `has_funding_yml` | a FUNDING.yml file exists for the repo or its owner, even if it resolves to no links |
| `funding_link_platforms` | declared platform keys (comma-separated) |
| `has_funding_json` | repo, or its owner via an org-level manifest, is in the FLOSS Fund directory — a declared channel (caps `score` at 79) |
| `has_npm_funding`, `npm_funding_url` | npm package.json `funding` field — a declared channel (caps at 79) |
| `has_pypi_funding`, `pypi_funding_platforms` | PyPI `project_urls` funding link — a declared channel (caps at 79) |
| `paypal` | curated PayPal.me URL from `overrides.csv` — a declared channel (feeds `intent` + `channels_count`, caps at 79); empty when none |
| `bf_maintainer_fundable` | a bus factor maintainer has a personal GitHub Sponsors listing (∪ curated overrides); intent-only, no cap |
| `channels_count` | distinct funding platforms (links ∪ funding.json repo+org channels ∪ npm ∪ pypi ∪ paypal) |
| `oc_slug` | attributed Open Collective slug (override / reverse-map); empty when none |
| `oc_avg_funding` | mean OC gross annual budget (`0` if none) |
| `oc_avg_funding_p` | risk percentile of `oc_avg_funding` |
| `host` | legally-connected steward — a roster code from a scraped roster, a domain from a curated file, or empty |
| `host_type` | `company` / `nonprofit` / empty |
| `owner` | owning-entity domain (e.g. `meta.com`), from `overrides.csv` |
| `owner_type` | `company` / `nonprofit` / empty |
| `host_score` | `min(type(host), type(owner))` ∈ {`0` company, `0.5` nonprofit, `1` none} — the third score axis (×100) |
| `score` | funding-risk score, int 0–100 |
| `intent`, `nonprofit` | the two boolean eligibility flags |

### `intent` and `nonprofit` → `data/eligibility/eligibility.csv`

`src/eligibility/build_eligibility.py` joins only these two boolean columns.
They are two of the four checks behind
`eligible = oss AND intent AND nonprofit AND active`
(see [eligibility.md](../eligibility.md)). The funding `score` contributes to no
aggregate — neither the eligibility rollup nor `risk.csv`'s `risk_score` (the
geometric mean of `concentration`, `complexity`, `security`, `workload`).

**`intent`** (`bool`, default `false`) is true on any one of: `gh_sponsors_enabled`,
`has_funding_links`, `has_funding_yml`, `has_funding_json`, `has_npm_funding`,
`has_pypi_funding`, `bf_maintainer_fundable`, a non-empty `paypal`, `oc_slug`,
`host`, or `owner`. The inbound sponsor count is not tested separately — any
count implies the listing is enabled. **Outbound sponsoring is not intent**:
funding others is not a channel for this repo.

`intent` then **propagates at the owner level** (`_propagate_owner_intent`).
Once any repo an owner has in scope declares a channel from
`DECLARED_CHANNEL_COLS` (`gh_sponsors_enabled`, `has_funding_links`,
`has_funding_yml`, `has_funding_json`, `has_npm_funding`, `has_pypi_funding`) or
carries an `oc_slug`, every repo that owner has is set `intent=true`. Curated
host/owner backing does not propagate. This mirrors GitHub's own semantics: an
org's `.github/FUNDING.yml` and a personal Sponsors listing already apply to all
of an owner's repos, so `serde-rs/json` counts as fundable while sibling
`serde-rs/serde` declares `github: dtolnay`. Propagation only *adds* intent.

**`nonprofit`** (`bool`, default `true`) is false only when `host_type` or
`owner_type == "company"`. Propagation never touches it, so a company-owned org
stays ineligible. Corporate-backed repos keep their `eligibility.csv` row
flagged `nonprofit=false` — ineligible, but not hidden.

## Coverage

See the preview pipeline sheet → Eligibility → Intent and nonprofit.

## Limitations

- **Off-platform money is invisible unless curated.** The two scored channel
  axes are GitHub Sponsors and OpenCollective; `host_score` adds institutional
  backing. A repo with no curated override and no roster host still shows no
  corporate payroll, grant or VC signal, except through the outbound-sponsoring
  proxy. `astral-sh/ruff` is the canonical case: VC-backed with
  `gh_sponsorships_in=0`, corrected only by a curated `astral.sh` owner
  override.
- **funding.json carries no dollars.** `has_funding_json` feeds `intent` and the
  declared-channel cap only.
- **`score` is a percentile, not a class.** It lives in `funding.csv` and feeds
  no aggregate.
- **OC is the only dollar amount.** GitHub Sponsors and Patreon/Tidelift amounts
  are not public, so dollar figures exist only for repos with an attributed Open
  Collective. The sponsorship axis is a *count*, not a *sum*.
