"""Shared git-URL helpers: canonicalization + non-GitHub reachability cache."""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console

from src.common.params import fetch_ttl_days

console = Console()

# ── Paths / constants ──────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_VALIDITY_CACHE = DATA_DIR / "sources" / "git" / "urls.csv"

GIT_URL_TTL_DAYS = fetch_ttl_days("value/git_urls")  # 365 days, from settings.json
LS_REMOTE_TIMEOUT = 25
LSREMOTE_PARALLEL = 5

_VALIDITY_FIELDS = ["url", "valid", "method", "checked_at"]

# Hosts that don't support the unauthenticated `git://` daemon — keep / fall
# back to https://. Most modern GitLab installs, Bitbucket, KDE Invent,
# Phabricator, Codeberg, OpenLDAP, postgresql.org, qemu.org, etc.
HTTPS_ONLY_HOST_PREFIXES: tuple[str, ...] = (
    "gitlab.",                  # GitLab Enterprise / .com
    "invent.kde.org",
    "salsa.debian.org",
    "code.videolan.org",
    "bitbucket.org",
    "dev.gnupg.org",            # Phabricator
    "chromium.googlesource.com",  # gitiles
    "aomedia.googlesource.com",
    # Hosts that historically supported git:// but disabled it later:
    "git.openldap.org",
    "git.postgresql.org",
    "codeberg.org",
    "git.libssh.org",
    "git.qemu.org",
    "pagure.io",
)


# ── URL canonicalization ───────────────────────────────────────────────────────

def _host(url: str) -> str:
    """Extract the host component from a URL, or '' if not parseable."""
    if "//" not in url:
        return ""
    return url.split("//", 1)[1].split("/", 1)[0]


def _canonicalize_git_url(url: str) -> str:
    """Rewrite known-wrong git URL patterns to their canonical clone form.

    Pipeline of rewrites — each rule UPDATES `u` rather than returning
    early, so post-processing (github-strip, https→git:// conversion)
    runs unconditionally at the end.

    Returns the cleaned URL, or `""` if there's no git equivalent
    (non-git VCS like Mercurial, dead servers, github URL — github URLs
    are tracked via the `repo` / `platform` columns).
    """
    u = (url or "").strip()
    if not u:
        return ""

    # ── Pattern rewrites (mechanical) ─────────────────────────────────
    # Strip gitweb display-suffixes (;a=summary, ;a=tree, etc.)
    u = re.sub(r";[a-z]=[\w]+(;[a-z]=[\w]+)*\b", "", u)

    # Savannah cgit/git web view → git:// (HTTPS smart-http is broken/slow,
    # git:// works instantly; path drops the `/git/` prefix).
    m = re.match(r"^https?://git\.savannah\.(gnu|nongnu)\.org/(?:cgit|git)/(.+)$", u)
    if m:
        u = f"git://git.savannah.{m.group(1)}.org/{m.group(2)}"

    # kernel.org cgit web view → smart-http git endpoint at /pub/scm/...
    m = re.match(r"^https?://git\.kernel\.org/cgit/(.+?)(?:\.git/?)?$", u)
    if m:
        u = f"https://git.kernel.org/pub/scm/{m.group(1)}.git"

    # GnuPG gitweb → use gpg/* GitHub mirror (their smart-http is broken)
    m = re.match(r"^https?://git\.gnupg\.org/cgi-bin/gitweb\.cgi\?p=([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # GnuPG direct .git URL → also use gpg/* GitHub mirror
    m = re.match(r"^https?://git\.gnupg\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # Sourceware project landing page → git endpoint at /git/X.git
    m = re.match(r"^https?://sourceware\.org/([a-z][\w-]*)/?$", u)
    if m and m.group(1) not in ("git", "cgit", "elfutils-doc"):
        u = f"https://sourceware.org/git/{m.group(1)}.git"

    # VideoLAN gitweb → code.videolan.org GitLab
    m = re.match(r"^https?://git\.videolan\.org/\?p=([\w.-]+)\.git", u)
    if m:
        u = f"https://code.videolan.org/videolan/{m.group(1)}.git"

    # Linux-NFS gitweb (?p=user/proj.git) → git:// form
    m = re.match(r"^https?://git\.linux-nfs\.org/\?p=([^/]+)/([\w.-]+)\.git", u)
    if m:
        u = f"git://git.linux-nfs.org/projects/{m.group(1)}/{m.group(2)}.git"

    # freedesktop anongit → gitlab.freedesktop.org
    m = re.match(r"^https?://anongit\.freedesktop\.org/git/(.+)\.git$", u)
    if m:
        u = f"https://gitlab.freedesktop.org/{m.group(1)}.git"

    # OpenLDAP archive download URL → main repo
    if u.startswith("https://git.openldap.org/openldap/openldap/-/archive/"):
        u = "https://git.openldap.org/openldap/openldap.git"

    # Xiph old git.xiph.org → gitlab.xiph.org
    m = re.match(r"^https?://git\.xiph\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://gitlab.xiph.org/xiph/{m.group(1)}.git"

    # SourceForge legacy <project>.git.sourceforge.net → modern git.code.sf.net
    m = re.match(r"^https?://([\w-]+)\.git\.sourceforge\.net/?$", u)
    if m:
        u = f"https://git.code.sf.net/p/{m.group(1)}/code"

    # GitLab API URL (graphviz et al.) — not a git endpoint
    if u.startswith("https://gitlab.com/api/v4/") or "/api/v4/projects/" in u:
        u = "https://gitlab.com/graphviz/graphviz.git" if "4207231" in u else ""

    # Postgres canonical missing .git suffix
    if u == "https://git.postgresql.org/git/postgresql":
        u = "https://git.postgresql.org/git/postgresql.git"

    # libpthread-stubs path mismatch on gitlab.freedesktop.org
    if u == "https://gitlab.freedesktop.org/xorg/lib/libpthread-stubs.git":
        u = "https://gitlab.freedesktop.org/xorg/lib/pthread-stubs.git"

    # ── Post-processing (always runs) ─────────────────────────────────
    if not u:
        return ""

    # Non-git VCS — blank out, no canonical git exists
    if u.startswith(("https://hg.", "http://hg.",
                     "https://foss.heptapod.net/",
                     "https://code.launchpad.net/",
                     "https://core.tcl-lang.org/",
                     "https://gmplib.org/repo/",
                     "https://bitbucket.org/stoneleaf/",
                     "https://www.bytereef.org/")):
        return ""

    # Drop the raw github URL here; the verification loop re-derives the
    # canonical github clone URL from the (post-rename) `repo` slug, so every
    # github repo ends up with both an owner/repo slug and a git_url.
    if re.match(r"^https?://github\.com/", u):
        return ""

    # Standardise on git:// scheme for non-github URLs unless the host
    # explicitly disabled the unauthenticated git daemon — those hosts
    # are listed at module level in HTTPS_ONLY_HOST_PREFIXES.
    host = _host(u)
    https_only = any(host == h or host.startswith(h)
                     for h in HTTPS_ONLY_HOST_PREFIXES)

    if https_only:
        # Convert any scheme to https:// for these hosts
        if u.startswith("git://"):
            return "https://" + u[len("git://"):]
        if u.startswith("http://"):
            return "https://" + u[len("http://"):]
        return u
    # Otherwise prefer git:// — it's faster and tends to work on traditional
    # git daemons (Savannah, kernel.org, sourceware, sourceforge, …).
    if u.startswith("https://"):
        return "git://" + u[len("https://"):]
    if u.startswith("http://"):
        return "git://" + u[len("http://"):]
    return u


# ── Non-GitHub reachability + TTL validity cache ───────────────────────────────

def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_validity_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            u = (r.get("url") or "").strip()
            if u:
                out[u] = r
    return out


def _save_validity_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDITY_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for u in sorted(cache.keys()):
            w.writerow(cache[u])
    os.replace(tmp, path)


def _is_fresh(checked_at: str) -> bool:
    if not checked_at:
        return False
    try:
        when = dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=GIT_URL_TTL_DAYS)
        return when >= cutoff
    except Exception:
        return False


def _lsremote_pass(urls: list[str]) -> dict[str, bool]:
    """Definitive `git ls-remote` check for the ambiguous URLs."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_HTTP_LOW_SPEED_LIMIT": "500",
        "GIT_HTTP_LOW_SPEED_TIME": "20",
    }

    def _check(url: str) -> tuple[str, bool]:
        try:
            r = subprocess.run(
                ["git", "ls-remote", "--exit-code", url, "HEAD"],
                capture_output=True, timeout=LS_REMOTE_TIMEOUT,
                env=env, text=True,
            )
            return url, (r.returncode == 0 and bool(r.stdout.strip()))
        except subprocess.TimeoutExpired:
            return url, False
        except Exception:
            return url, False

    out: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=LSREMOTE_PARALLEL) as ex:
        futs = {ex.submit(_check, u): u for u in urls}
        for fut in as_completed(futs):
            u, ok = fut.result()
            out[u] = ok
    return out


def _verify_non_github(urls: list[str],
                       cache_path: Path = GIT_VALIDITY_CACHE,
                       force: bool = False) -> dict[str, bool]:
    """`git ls-remote` verification for non-github URLs, with TTL cache."""
    cache = _load_validity_cache(cache_path)
    now = _now_iso()

    fresh: dict[str, bool] = {}
    to_check: list[str] = []
    for u in urls:
        c = cache.get(u)
        if not force and c and _is_fresh(c.get("checked_at", "")):
            fresh[u] = (c.get("valid", "").lower() == "true")
        else:
            to_check.append(u)

    console.print(
        f"  [dim]non-github URLs:[/dim] {len(urls):,} unique  "
        f"[green]{len(fresh):,} cached[/green]  "
        f"[yellow]{len(to_check):,} to verify[/yellow]"
    )

    new_results: dict[str, bool] = {}
    if to_check:
        t0 = time.monotonic()
        results = _lsremote_pass(to_check)
        ok = sum(1 for v in results.values() if v)
        console.print(
            f"  [dim]ls-remote ({time.monotonic()-t0:.1f}s):[/dim] "
            f"valid={ok}/{len(to_check)}"
        )
        for u, v in results.items():
            new_results[u] = v
            cache[u] = {"url": u, "valid": str(bool(v)),
                        "method": "ls-remote", "checked_at": now}
        _save_validity_cache(cache_path, cache)
    return {**fresh, **new_results}
