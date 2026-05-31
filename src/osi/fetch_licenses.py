"""Fetch the OSI-approved license list and write to CSV.

Inclusion rule:  `isOsiApproved`  ∪  `EXTRAS`.

Strict OSS (software open source), **not** FSF "free software". The
distinction matters: FSF's `isFsfLibre` flag covers content licenses
(CC-BY-4.0, CC0-1.0, GFDL) that are free for documents/data but
aren't software OSS in the OSI sense. Excluding them keeps the
endowment's eligibility scope to real software projects.

`EXTRAS` covers a handful of licenses that are universally treated as
software OSS but never went through OSI's formal review (curl, ftl,
libpng-2.0, mit-cmu, psf-2.0, blessing) — see the dict below.

Why SPDX (and not `api.opensource.org/licenses`):

  • SPDX is the authoritative SPDX-ID source — its IDs match what npm,
    PyPI, crates, and Homebrew publishers actually declare.
  • The OSI's own JSON API was returning empty bodies during testing.
  • SPDX includes the `isOsiApproved` flag and a `seeAlso` list that
    typically contains the OSI license page URL — best of both.

Output: `data/sources/osi/oss-licenses.csv`. Columns:

  spdx_id           — lowercased SPDX ID (the join key, e.g. `apache-2.0`)
  spdx_id_canonical — original SPDX casing (`Apache-2.0`)
  name              — full human-readable name
  source            — `osi` or `extras` (the rule that admitted it)
  is_deprecated     — `True` if SPDX deprecated this ID (still OSS but legacy)
  reference         — `https://spdx.org/licenses/{id}.html`
  osi_url           — first `https://opensource.org/license/...` link from seeAlso
  fetched_at        — UTC ISO 8601 timestamp

90-day TTL — re-runs within the window are no-ops unless `--force`.

Usage:
    uv run python -m src.osi.fetch_licenses
    uv run python -m src.osi.fetch_licenses --force
"""

import argparse
import csv
import datetime as dt
import logging
import os
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "sources" / "osi"
OUT = DATA_DIR / "oss-licenses.csv"

SPDX_LIST_URL = (
    "https://raw.githubusercontent.com/spdx/license-list-data/main/json/licenses.json"
)
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TTL_DAYS = 90

FIELDS = ["spdx_id", "spdx_id_canonical", "name", "source", "is_deprecated",
          "reference", "osi_url", "fetched_at"]

# SPDX IDs that are universally treated as software OSS but never made
# it onto OSI's official list — almost always because the author never
# bothered to file paperwork, not because of any substantive concern.
# Each entry below carries the canonical project/person it covers, the
# specific reason OSI's list omits it, and authoritative source URLs so
# the policy is auditable. New entries here loosen eligibility, so add
# only with comparable evidence.
EXTRAS: dict[str, str] = {
    # ── curl ────────────────────────────────────────────────────────
    # The curl/libcurl project, started by Daniel Stenberg in 1996.
    # curl is the canonical example of OSS critical infrastructure
    # (xkcd #2347 "Dependency"); it ships preinstalled in macOS, iOS,
    # Windows 10+ (Microsoft bundled it in 2017), every Linux distro,
    # PlayStation/Xbox, every car infotainment system tested, and on
    # NASA/JPL Mars rovers. Stenberg won the Polhem Prize (2017) and
    # has been keynoting FOSDEM, Open Source Summit, etc. for decades.
    # The license is a tiny custom MIT/X11 derivative whose only
    # variation from MIT is a "no use of the curl name to endorse"
    # clause — substantively equivalent to MIT for OSS purposes
    # (Debian, Fedora, FSF, SPDX all accept it as free). OSI never
    # reviewed it because nobody submitted it; that finally changed
    # 2026-02-03 when Christoph Röth filed a review with OSI's
    # license-review committee, with Stenberg's explicit permission
    # the same day. Once OSI approves and SPDX flips
    # `isOsiApproved=true`, the 90-day refetch will move this entry
    # from `extras` to `osi` automatically.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/curl.html
    #   curl:        https://curl.se/docs/copyright.html
    #   OSI thread:  https://curl.se/mail/lib-2026-02/0001.html
    #   FSF list:    https://www.gnu.org/licenses/license-list.html#curl
    #   Wikipedia:   https://en.wikipedia.org/wiki/CURL
    #                https://en.wikipedia.org/wiki/Daniel_Stenberg
    "curl":        "MIT/X11 derivative; curl (Daniel Stenberg). "
                   "OSI submission filed 2026-02 (Christoph Röth)",

    # ── ftl (FreeType Project License) ──────────────────────────────
    # The FreeType project, started by David Turner in 1996 and led
    # for the past two decades by Werner Lemberg. FreeType is the
    # de-facto font rasterizer of the open-source world: it ships in
    # every Linux distro, Android (since 1.0), ChromeOS, FreeBSD,
    # NetBSD, Haiku, GhostScript, WebKit/Safari (until Apple's own
    # rasterizer took over), and is dual-licensed so commercial
    # consumers can pick the FTL or GPLv2. The FTL itself is a
    # BSD-style attribution license drafted to satisfy David Turner's
    # specific concern that derived works credit FreeType prominently
    # — substantively in the BSD/MIT family. FSF marks it free
    # (`isFsfLibre=true`); OSI never reviewed it because the FreeType
    # team didn't submit. Debian and Fedora both ship FreeType in
    # main/`free`.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/FTL.html
    #   FreeType:    https://freetype.org/license.html
    #   FSF list:    https://www.gnu.org/licenses/license-list.html#FreeType
    #   Wikipedia:   https://en.wikipedia.org/wiki/FreeType
    "ftl":         "FreeType Project License — FreeType "
                   "(David Turner, Werner Lemberg)",

    # ── libpng-2.0 ──────────────────────────────────────────────────
    # The libpng reference library, the canonical PNG codec used by
    # every major browser (Chromium, Firefox, Safari, Edge), every
    # major OS (macOS, iOS, Android, Windows via WIC, Linux), Adobe
    # apps, GIMP, GhostScript, and ImageMagick. PNG itself is a W3C
    # Recommendation and ISO/IEC 15948 standard; libpng is the
    # reference implementation. Long led by Glenn Randers-Pehrson
    # (1996–2019); current maintainer is Cosmin Truța. The 2.0
    # license replaced the older multi-clause libpng-1.x license in
    # 2018 with a simpler permissive form modelled on zlib/MIT — no
    # OSI submission was made because the libpng team considered the
    # change purely editorial.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/libpng-2.0.html
    #   libpng:      http://www.libpng.org/pub/png/libpng.html
    #   PNG (W3C):   https://www.w3.org/TR/PNG/
    #   Wikipedia:   https://en.wikipedia.org/wiki/Libpng
    #                https://en.wikipedia.org/wiki/Portable_Network_Graphics
    "libpng-2.0":  "Permissive (zlib/MIT-style); libpng "
                   "(Cosmin Truța, originally Glenn Randers-Pehrson)",

    # ── mit-cmu ─────────────────────────────────────────────────────
    # Carnegie Mellon University's MIT-style license. Used most
    # visibly by Pillow (the actively-maintained fork of the Python
    # Imaging Library, PIL — originally written by Fredrik Lundh and
    # forked into Pillow by Alex Clark in 2010, now maintained by
    # Andrew Murray and a wide contributor base). Pillow is the
    # universal Python imaging library — a transitive dependency of
    # roughly the entire scientific-Python and ML stack (matplotlib,
    # scikit-image, torchvision, tensorflow). The license itself is
    # MIT with the customary CMU-research-code attribution wording,
    # substantively equivalent to MIT. OSI never reviewed it.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/MIT-CMU.html
    #   Pillow:      https://python-pillow.org/
    #   Wikipedia:   https://en.wikipedia.org/wiki/Python_Imaging_Library
    #                https://en.wikipedia.org/wiki/Fredrik_Lundh
    "mit-cmu":     "MIT variant (CMU); Pillow "
                   "(Andrew Murray; originally Fredrik Lundh's PIL)",

    # ── psf-2.0 ─────────────────────────────────────────────────────
    # Python Software Foundation License 2.0. Functionally identical
    # to the OSI-approved `python-2.0` (the same license text under a
    # different SPDX id used after CNRI's contributions had been
    # cleanly separated). Maintained by the Python Software
    # Foundation, the 501(c)(3) that stewards CPython since Guido van
    # Rossum (Python BDFL until 2018) handed governance to the PSF.
    # Most "Python license" projects (typing-extensions, backports.*,
    # PSF-maintained tooling) declare `PSF-2.0` rather than
    # `Python-2.0`. SPDX still flags PSF-2.0 as not-OSI-approved
    # because OSI only formally lists the older `Python-2.0` text;
    # in practice both are the OSS Python license.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/PSF-2.0.html
    #   Python:      https://docs.python.org/3/license.html
    #   PSF:         https://www.python.org/psf/
    #   Wikipedia:   https://en.wikipedia.org/wiki/Python_Software_Foundation_License
    #                https://en.wikipedia.org/wiki/Python_Software_Foundation
    #                https://en.wikipedia.org/wiki/Guido_van_Rossum
    "psf-2.0":     "PSF License 2.0 — functionally equivalent to OSI-"
                   "approved python-2.0 (Python Software Foundation)",

    # ── blessing ────────────────────────────────────────────────────
    # SQLite's public-domain dedication, written by D. Richard Hipp.
    # SQLite is by Hipp's own count the most-deployed database engine
    # in the world: every iOS and Android device, every major web
    # browser, macOS, Windows 10+, every Airbus A350, every Boeing
    # 787, Skype, Dropbox, and the Library of Congress's archival
    # format (SQLite is one of the formats the LoC recommends for
    # long-term preservation, alongside CSV and PDF/A). The
    # "Blessing" license is a public-domain dedication that closes
    # with Hipp's well-known benediction ("May you do good and not
    # evil. May you find forgiveness for yourself and forgive others.
    # May you share freely, never taking more than you give."). Hipp
    # deliberately avoided OSI/FSF licenses because he wanted the
    # strongest possible PD dedication; commercial support is offered
    # via the SQLite Consortium (Adobe, Bentley, Bloomberg, Mozilla,
    # etc.). SQLite is a textbook OSS-infrastructure project; the
    # only reason `blessing` isn't OSI-approved is that Hipp never
    # submitted a public-domain-style dedication to OSI's process.
    # Sources:
    #   SPDX:        https://spdx.org/licenses/blessing.html
    #   SQLite:      https://www.sqlite.org/copyright.html
    #   Consortium:  https://www.sqlite.org/consortium.html
    #   LoC:         https://www.loc.gov/preservation/digital/formats/fdd/fdd000461.shtml
    #   Wikipedia:   https://en.wikipedia.org/wiki/SQLite
    #                https://en.wikipedia.org/wiki/D._Richard_Hipp
    "blessing":    "Public-domain dedication; SQLite "
                   "(D. Richard Hipp)",
}


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_fresh(path: Path) -> bool:
    """Return True if `path` exists and its newest `fetched_at` is within TTL."""
    if not path.exists():
        return False
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = r.get("fetched_at", "")
            if not ts:
                continue
            try:
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.UTC)
                return when >= cutoff
            except Exception:
                pass
    return False


def fetch() -> list[dict]:
    log.info("fetching SPDX license list ...")
    resp = httpx.get(SPDX_LIST_URL, timeout=30,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()

    rows: list[dict] = []
    now = _now_iso()
    seen: set[str] = set()
    by_id = {l.get("licenseId", "").lower(): l for l in data.get("licenses", [])}

    def _add(lic: dict, source: str) -> None:
        spdx = lic.get("licenseId", "")
        key = spdx.lower()
        if key in seen:
            return
        seen.add(key)
        # Pick the OSI URL from seeAlso when available — otherwise empty.
        osi_url = ""
        for u in lic.get("seeAlso") or []:
            if "opensource.org/license" in u or "opensource.org/licenses" in u:
                osi_url = u
                break
        rows.append({
            "spdx_id": key,
            "spdx_id_canonical": spdx,
            "name": lic.get("name", ""),
            "source": source,
            "is_deprecated": bool(lic.get("isDeprecatedLicenseId", False)),
            "reference": lic.get("reference", ""),
            "osi_url": osi_url,
            "fetched_at": now,
        })

    # Pass 1: every OSI-approved license.
    for lic in data.get("licenses", []):
        if lic.get("isOsiApproved"):
            _add(lic, source="osi")
    # Pass 2: hand-curated extras (universally OSS, OSI never reviewed).
    for spdx_lc in EXTRAS:
        lic = by_id.get(spdx_lc)
        if lic is None:
            log.warning("EXTRAS entry %s not found in SPDX list — skipping", spdx_lc)
            continue
        _add(lic, source="extras")

    rows.sort(key=lambda r: r["spdx_id"])
    return rows


def _write(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUT)


def ensure(force: bool = False, verbose: bool = True) -> Path:
    """Ensure `data/sources/osi/oss-licenses.csv` exists and is fresh.

    Called by `pipeline.eligibility` at startup so the pipeline is
    self-bootstrapping — no need to remember to run the fetcher first.
    Returns the resolved path.

    Honors the same 90-day TTL as the CLI: a re-call on a fresh file is a
    no-op. Set `force=True` to ignore the TTL.
    """
    if not force and _is_fresh(OUT):
        if verbose:
            console.print(
                f"[dim]osi/oss-licenses.csv: fresh — skipping fetch[/dim]")
        return OUT

    if verbose:
        console.print(
            f"[dim]osi/oss-licenses.csv: "
            f"{'forced refresh' if force else 'missing or stale'} → "
            f"fetching SPDX list[/dim]")
    rows = fetch()
    _write(rows)
    if verbose:
        n = len(rows)
        by_src = {s: sum(1 for r in rows if r["source"] == s)
                  for s in ("osi", "extras")}
        deprecated = sum(1 for r in rows if r["is_deprecated"])
        console.print(
            f"[dim]  → {n:,} OSS SPDX ids "
            f"(osi={by_src['osi']}, extras={by_src['extras']}, "
            f"deprecated={deprecated}) written to {OUT}[/dim]")
    return OUT


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help="ignore 90-day TTL and re-fetch")
    args = p.parse_args()

    console.rule("[bold cyan]osi/fetch_licenses")
    console.print(f"  Source: [dim]{SPDX_LIST_URL}[/dim]")
    console.print(f"  TTL:    [dim]{TTL_DAYS} days[/dim]   "
                  f"force=[dim]{args.force}[/dim]")

    if not args.force and _is_fresh(OUT):
        console.print(f"  [green]✓ fresh[/green] — keeping [cyan]{OUT}[/cyan]")
        return

    rows = fetch()
    _write(rows)

    n = len(rows)
    by_src = {s: sum(1 for r in rows if r["source"] == s)
              for s in ("osi", "extras")}
    deprecated = sum(1 for r in rows if r["is_deprecated"])
    console.print(f"  Fetched [bold]{n:,}[/bold] OSS SPDX ids "
                  f"(osi={by_src['osi']}, extras={by_src['extras']}, "
                  f"deprecated={deprecated})")
    console.print(f"  → wrote [cyan]{OUT}[/cyan]")

    # Show a sample
    tbl = Table(title="[bold]Sample (first 12 by SPDX id)[/bold]",
                header_style="bold dim", padding=(0, 1))
    tbl.add_column("spdx_id"); tbl.add_column("name")
    for r in rows[:12]:
        tbl.add_row(r["spdx_id"], r["name"][:55])
    console.print(tbl)


if __name__ == "__main__":
    main()
