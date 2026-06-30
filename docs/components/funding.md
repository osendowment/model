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
| GitHub Sponsors enabled | owner has a Sponsors listing — `has_gh_sponsors` (a channel exists even at 0 sponsors) | github |
| GitHub Sponsors received | owner has ≥1 sponsor — `gh_sponsorships_in > 0` (owner-only; co-maintainers don't count) | github |
| FUNDING.yml link | repo declares a funding link — `has_funding_link` | github |
| FLOSS Fund manifest | repo registered, or owner has an org-level manifest — `has_funding_json` / `org_fundable` | floss_fund |
| npm funding | npm `package.json` `funding` field — `has_npm_funding` | npm |
| PyPI funding | PyPI `project_urls` funding entry — `has_pypi_funding` | pypi |
| Open Collective | repo/org maps to a real collective — `oc_slug` | opencollective |
| Institutional host | a foundation/company legally stewards it — `host` | foundations, overrides |
| Institutional owner | an owning entity is recorded — `owner` | overrides |

## Nonprofit

`nonprofit=true` by default; **false** only when a corporate entity (Meta,
Google, Microsoft, AWS, …) hosts or owns the repo.

| Criteria | Sources |
|---|---|
| `host_type == company` (corporate steward) | foundations, overrides |
| `owner_type == company` (corporate owner) | overrides |

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
| `opencollective/` | `opencollective.fetch_collectives` + `fetch_budgets` | OC collective↔repo map + gross annual budgets |
| `npm/funding.csv` | `npm.fetch_funding` | npm `package.json` `funding` field |
| `pypi/funding.csv` | `pypi.fetch_funding` | PyPI `project_urls` funding entry |
| `funding/host-by-repo.csv` | foundation scrapers (`sources/funding/`) | scraped FOSS-foundation host per repo |
| `funding/overrides.csv` | curated | per-repo/org `host`/`owner` backing + `oc_slug` (schema `repo,host,host_type,gh_user,owner,owner_type,oc_slug`; `owner/*` rows apply org-wide) |
| → `risk/funding.csv` | `risk.build_funding` | joins all sources → `intent`, `nonprofit` (+ a signal-only funding `score`) |
