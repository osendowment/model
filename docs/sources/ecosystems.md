# ecosyste.ms

[ecosyste.ms](https://ecosyste.ms) is a cross-registry index of open-source packages and
repositories. The model uses four connectors/extractors against it, all under
`src/sources/ecosystems/`.

> **Status:** the criticality connector's `critical` flag now feeds the **Value** stage as
> `value.csv`'s `eco_crit` column — the 0.2-weight component of `value_score`, and the only
> importance signal GitLab class-A repos carry (see [value.md](../value.md), applied by
> `src.value.apply_criticality`). Coverage counts live in the preview stats sheet; this page
> describes **how** the data is fetched, not **how many**.

## Packages connector (URL backfill)

`src/sources/ecosystems/packages.py` — for packages the native registry crawl left without a
repo URL, queries `packages.ecosyste.ms/api/v1/registries/{registry}/packages/{name}` and writes
`data/sources/ecosystems/{eco}/packages.csv` (`package, registry_hit, repository_url, homepage,
fetched_at`). Used by the Value stage to fill missing `github_repo` / `git_url`.

## Candidates connector (class-A audit fetch)

`src/sources/ecosystems/candidates.py` — unlike `packages.py` (which only backfills packages
missing both a `github_repo` and a `git` URL), this fetches ecosyste.ms data for **every
class-A candidate** package, to obtain an *independent* repository identity that
`src/value/audit_ecosystems.py` compares against ours (that audit writes
`data/sources/ecosystems/audit.csv`). It shares the raw-JSON cache with `packages.py`
(`data/sources/ecosystems/{eco}/raw/{package}.json`) and writes the consolidated
cross-ecosystem index `data/sources/ecosystems/packages.csv` (`ecosystem, package, purl,
registry_hit, repository_url, homepage, repo_host, repo_full_name, repo_archived, repo_fork,
repo_stars, last_synced_at, fetched_at`). Incremental with a 1-year TTL.

## Maintainers extractor

`src/sources/ecosystems/fetch_maintainers.py` — re-reads the raw-JSON cache (no network
calls) and extracts the registry-native `maintainers` arrays into
`data/sources/ecosystems/maintainers.csv` (`ecosystem, package, repo_id, login, name, email,
role, uuid, html_url, fetched_at`; `repo_id` resolved from `packages.csv`'s
`repo_full_name`). Only npm/pypi/crates.io expose maintainers on ecosyste.ms — cpp's
registries do not, so cpp packages are skipped.

## Criticality connector

`src/sources/ecosystems/criticality.py` — an **importance factor** for the top repos, written to
`data/sources/ecosystems/criticality.csv` (one row per repo).

### Why package-level, keyed to repos

ecosyste.ms exposes its criticality signals only on the **packages** API: a `critical` boolean
(critical-list membership), a `rankings` object (per-registry percentiles), and dependent counts.
The **repos** API carries none of them (only basic metadata + an optional cached OpenSSF
Scorecard). So criticality is inherently package-level.

Rather than reverse-look a repo's packages by URL — noisy (a monorepo returns 100+ junk claimants,
capped at 100, and the endpoint 500s on them) — this connector fetches the repo's **canonical
package**: the `top_eco` / `top_eco_pkg` the Value stage already resolved. Every row is therefore
the criticality of the one package that defines the repo, and it works identically for GitHub and
GitLab-hosted repos (a GitLab C library resolves as its `cpp` package).

One deliberate exception: the `critical` flag is curated per registry package and many registries
omit it (spack/conan/debian, plenty of npm/pypi/crates packages). When the canonical package
resolves without a flag, a `packages/lookup?repository_url=` fallback scans the repo's **other**
packages for an **explicit** flag (conda's `openssl` where spack has none; npm's `vitest` where
`@vitest/pretty-format` carries nothing). Only explicit `critical` values are taken — a flag is
never derived from `rank_average` — and all numeric fields still come from the canonical package
alone. Lookup responses are cached (trimmed) under `raw/criticality/lookup/`, same TTL.

### Registry resolution

`top_eco → registry`: `npm → npmjs.org`, `pypi → pypi.org`, `crates → crates.io`. For `cpp` the
order is **`spack.io, conan.io, vcpkg.io, debian-13, debian-12`** — this deliberately diverges from
`packages.py` (which tries debian first for widest URL coverage): ecosyste.ms does **not** rank
distro registries, so every debian package reports `rank_average=100`, whereas spack/conan/vcpkg
*do* rank (e.g. `bzip2` = 0.38 on spack vs 100 on debian). debian is kept last, as a coverage
fallback for packages the ranked registries don't carry.

### Columns (`criticality.csv`)

`repo_id` (`gh/{id}` / `gl/{nickname}-{id}` / `gl/{id}`, read straight from value.csv's unified id),
`repository_url` (the row key), `class`,
`valid` (value.csv's `git_valid`, carried so the scope stays filterable), `ecosystem` (`top_eco`),
`package` (`top_eco_pkg`), `registry_hit` (which registry served it), `ok` (success flag), `error`
(reason when `ok=False`), `critical` (`True`/`False`/blank when no registry states it),
`critical_via` (registry that supplied the flag — `registry_hit` when the canonical package carried
it, another registry when the lookup fallback found it, blank when none),
`rank_average` (percentile; **lower = more important**, e.g. express ≈ 0.08),
`dependent_repos_count`, `dependent_packages_count`, `fetched_at`. Blank ≠ 0 throughout — a blank
`critical` / `rank_average` / dependent count means the registry omitted the field (unknown), never
a real zero.

`ok=False` carries its reason in `error`: `not-found-in-any-registry` (a stable value-stage ↔
registry name mismatch) vs `fetch-error` (a transient HTTP 5xx / timeout / bad JSON, retried on the
next run). A network blip is therefore never silently recorded as a genuine miss (auditability).

### Scope

Scoped on **value class alone** (default A), *not* via `src.common.repos.load_top_repos`: that
loader now spans GitHub + GitLab, but it gates on `git_valid == True`, which would drop
not-yet-valid class-A repos. Criticality is a package-importance
factor, not a risk-completeness gate, so a not-yet-`valid` or archived class-A repo still has a
meaningful criticality. Each row carries `valid` (value.csv's `git_valid`) so a consumer can
re-apply the gate.

### Package-name overrides

Occasionally the value stage's `top_eco_pkg` differs from the registry's canonical name
(GNU/spack naming, or a library shipping under a `lib*` name), so the package resolves nowhere.
`PACKAGE_OVERRIDES` in `criticality.py` (code, not a data file — it travels with the fetcher) maps
a `repository_url` to the correct registry name, e.g. `bdwgc/bdwgc → bdw-gc`, `…/cairo → cairo`,
`mpc/mpc → mpc`, `netflix/vmaf → libvmaf`. Before adding one, probe **all** cpp registries via the
`packages/lookup?repository_url=` endpoint to find the real name — a partial probe can wrongly look
"un-indexed" (e.g. `vmaf` is absent from spack/conan/debian but present as `libvmaf` on vcpkg).

### Caveats

- **`rank_average` is a per-registry percentile**, so a spack 0.38 and an npm 0.16 are both
  "top fraction of their registry" but not directly comparable in absolute importance — npm has
  millions of packages, spack a few thousand. Compare within an ecosystem, or use `critical` /
  `dependent_repos_count` for cross-ecosystem ordering.
- **Distro C libraries carry a weaker signal**: even on spack, `dependent_repos_count` is 0 for
  most (deps.dev doesn't track system-library reverse-deps), so their criticality rests on the
  rank percentile alone.

### Freshness & scope

90-day TTL on `fetched_at`; a re-run inside the window is a no-op, `--ttl 0` forces refresh. Scoped
to value class A by default (`--classes A B …`, or `all`); criticality is an importance signal for
the head of the distribution. Raw package JSON is cached per `(ecosystem, package)` under
`data/sources/ecosystems/raw/criticality/` for audit. This cache is deliberately **separate**
from `packages.py`'s (`data/sources/ecosystems/{eco}/raw/`) and the two cannot be merged: the
fetchers query cpp registries in different orders (debian-first for URL coverage vs spack-first
for a real ranking), and debian reports `rank_average=100` for every package — sharing the cache
would silently regress cpp rankings to 100 (see `_cache_path` in `criticality.py`).

### Scripts

- `uv run python -m src.sources.ecosystems.criticality` — class-A by default.
- `--classes A B` / `--limit N` / `--ttl 0` / `--concurrency N`.
