# ecosyste.ms

[ecosyste.ms](https://ecosyste.ms) is a cross-registry index of open-source packages and
repositories. The model uses two connectors against it, both under `src/sources/ecosystems/`.

> **Status:** collection layer. Both outputs are produced but **not yet consumed** by the Value
> or Risk stages — wiring the criticality factor into a stage is a follow-on. Coverage/funnel
> counts therefore live in `docs/stats.md` only once that wiring lands; this page describes
> **how** the data is fetched, not **how many**.

## Packages connector (URL backfill)

`src/sources/ecosystems/packages.py` — for packages the native registry crawl left without a
repo URL, queries `packages.ecosyste.ms/api/v1/registries/{registry}/packages/{name}` and writes
`data/sources/{eco}/raw/ecosystems.csv` (`package, registry_hit, repository_url, homepage,
fetched_at`). Used by the Value stage to fill missing `github_repo` / `git_url`.

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

### Registry resolution

`top_eco → registry`: `npm → npmjs.org`, `pypi → pypi.org`, `crates → crates.io`. For `cpp` the
order is **`spack.io, conan.io, vcpkg.io, debian-13, debian-12`** — this deliberately diverges from
`packages.py` (which tries debian first for widest URL coverage): ecosyste.ms does **not** rank
distro registries, so every debian package reports `rank_average=100`, whereas spack/conan/vcpkg
*do* rank (e.g. `bzip2` = 0.38 on spack vs 100 on debian). debian is kept last, as a coverage
fallback for packages the ranked registries don't carry.

### Columns (`criticality.csv`)

`repo_id` (`gh/{id}` / `gl/{host}/{id}`, best-effort), `repository_url` (the row key), `class`,
`valid` (value.csv's validity, carried so the scope stays filterable), `ecosystem` (`top_eco`),
`package` (`top_eco_pkg`), `registry_hit` (which registry served it), `ok` (success flag), `error`
(reason when `ok=False`), `critical` (`True`/`False`/blank when the registry omits it),
`rank_average` (percentile; **lower = more important**, e.g. express ≈ 0.08),
`dependent_repos_count`, `dependent_packages_count`, `fetched_at`. Blank ≠ 0 throughout — a blank
`critical` / `rank_average` / dependent count means the registry omitted the field (unknown), never
a real zero.

`ok=False` carries its reason in `error`: `not-found-in-any-registry` (a stable value-stage ↔
registry name mismatch) vs `fetch-error` (a transient HTTP 5xx / timeout / bad JSON, retried on the
next run). A network blip is therefore never silently recorded as a genuine miss (auditability).

### Scope

Scoped on **value class alone** (default A), *not* via `src.common.repos.load_top_repos`: that
loader's `valid` gate requires a `github_repo`, which would exclude every GitLab-hosted repo — but
GitLab repos are exactly what this signal is meant to cover. Criticality is a package-importance
factor, not a risk-completeness gate, so a not-yet-`valid` or archived class-A repo still has a
meaningful criticality. Each row carries `valid` so a consumer can re-apply the gate.

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
`data/sources/ecosystems/raw/criticality/` for audit.

### Scripts

- `uv run python -m src.sources.ecosystems.criticality` — class-A by default.
- `--classes A B` / `--limit N` / `--ttl 0` / `--concurrency N`.
