# Funding (risk component)

The funding component gathers every public signal that a top repo is set up to
receive support and derives two boolean flags joined into `risk.csv`:

- **`intent`** — the repo has expressed a way to be funded (a foundation/
  institutional host, or ≥1 funding channel). This is the endowment's **candidate
  filter**: it focuses on projects that have shown sustainability intent.
- **`nonprofit`** — the repo is *not* corporate-backed. Company-owned/hosted repos
  are kept but flagged `nonprofit=false` so they can be filtered out (already
  resourced).

Built by `src/risk/build_funding.py`. Current counts live in
[stats.md → Intent and nonprofit](../stats.md#intent-and-nonprofit).

## Intent

`intent=true` when **any** signal below is present. Only strict signals count — a
real funding channel or an institutional host. Outbound sponsoring (the owner
funding *others*) is **not** intent: it is not a funding channel for this repo.

| Signal | Criteria | Sources |
|---|---|---|
| `has_gh_sponsors` | the repo owner has GitHub Sponsors enabled — a listing exists even with zero sponsors | `github` |
| `gh_sponsorships_in` | the repo owner has at least one sponsor (owner only — co-maintainers are not counted) | `github` |
| `has_funding_link` | the repo declares a funding link in its FUNDING.yml | `github` |
| `has_funding_json` | the repo is registered in the FLOSS Fund directory | `floss_fund` |
| `org_fundable` | the repo's owner has an org-wide FLOSS Fund manifest | `floss_fund` |
| `has_npm_funding` | the repo's npm package declares a funding field | `npm` |
| `has_pypi_funding` | the repo's PyPI project declares a funding URL | `pypi` |
| `oc_slug` | the repo or its org maps to a real Open Collective | `opencollective` |
| `host` | a foundation or company legally stewards the repo | `foundations`, `overrides` |
| `owner` | a company or other entity owns the repo | `overrides` |

## Nonprofit

`nonprofit=true` by default; **false** only when a corporate entity (Meta,
Google, Microsoft, AWS, …) hosts or owns the repo.

| Criteria | Sources |
|---|---|
| a company legally stewards the repo (its host) | `overrides` |
| a company owns the repo | `overrides` |

## Data Pipeline

Each source is fetched into `data/sources/`, then `build_funding.py` joins them
onto the risk repos. Fetch order: funding-yml → sponsors → sponsorships →
floss-fund → opencollective → (npm/pypi) → build.

| Source | Fetcher | Gathers |
|---|---|---|
| `github/funding-yml.csv` | `github.fetch_funding_yml` | resolved FUNDING.yml funding links (platforms + handles) |
| `github/sponsors.csv` | `github.fetch_sponsors` | owner Sponsors enabled (`has_gh_sponsors`) + inbound count (`gh_sponsorships_in`, owner-only) |
| `github/sponsorships.csv` | `github.fetch_sponsorships` | owner outbound sponsoring count (score proxy only) |
| `floss-fund/funding-json.csv` | `floss_fund.funding_json` | FLOSS Fund manifest directory (repo + org-level) |
| `opencollective/collectives.csv` | `opencollective.fetch_collectives` | OC collective↔repo map |
| `opencollective/budgets.csv` | `opencollective.fetch_budgets` | gross annual budgets per collective |
| `npm/funding.csv` | `npm.fetch_funding` | npm `package.json` `funding` field |
| `pypi/funding.csv` | `pypi.fetch_funding` | PyPI `project_urls` funding entry |
| `funding/host-by-repo.csv` | foundation scrapers (`sources/funding/`) | scraped FOSS-foundation host per repo |
| `funding/overrides.csv` | curated | per-repo/org `host`/`owner` backing + `oc_slug` (schema `repo,host,host_type,gh_user,owner,owner_type,oc_slug`; `owner/*` rows apply org-wide) |
| → `risk/funding.csv` | `risk.build_funding` | joins all sources → `intent`, `nonprofit` (+ a signal-only funding `score`) |
