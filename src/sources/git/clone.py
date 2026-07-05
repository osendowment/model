"""Reusable git/tarball clone helpers for repo-level analysis.

Three strategies, picked by what the downstream tool needs:

1. ``download_tarball(repo, dest, client, ref="HEAD")``
   Streams a tar.gz from codeload.github.com and extracts to ``dest``.
   Fastest for small repos; no ``.git`` directory, so history-walking
   tools (gitpandas, truckfactor, ``git log``) won't work. Returns
   ``(elapsed_s, downloaded_bytes)``. Use for source-only analyses
   like scc, semgrep, lizard.

2. ``sparse_clone(repo, dest, size_kb=0, ref=None)``
   Sparse-checkout clone limited to source-file extensions
   (``SOURCE_EXTS``). Has ``.git`` but only a shallow snapshot
   (``--depth 1`` / single commit). Fetches only blobs matching the
   sparse patterns thanks to ``--filter=blob:none``. Returns
   ``(elapsed_s, pack_size_bytes)``. Use for source-only analyses
   on large repos where the tarball is wasteful.

3. ``bare_blobless_clone(repo, dest, ref=None)``
   Bare clone with ``--filter=blob:none --no-tags``. Pulls the full
   commit graph (every commit object) but no blobs — git lazy-fetches
   blobs on demand. Required for history-walking tools that need
   ``git log`` / commit traversal but not file contents (gitpandas,
   truckfactor). If ``ref`` is given, fetches it into a local branch
   ``_pinned`` so callers can pin analysis to a specific commit.
   Returns ``(elapsed_s, on_disk_bytes_after_clone)``.

All three create ``dest`` if missing and use the shared subprocess
runner that kills the whole process group on timeout.
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import tarfile
import tempfile
import time

import httpx

from src.sources.git.disk import CLONE_TMP_PREFIX

log = logging.getLogger(__name__)


# Glob patterns for sparse-checkout — one per source-file extension.
# Kept here so `sparse_clone` is self-contained; ``src.sources.git.fetch_scc``
# and other source-only fetchers re-export from this module.
SOURCE_EXTS: list[str] = [
    "*.c", "*.ec", "*.pgc", "*.h", "*.cc", "*.cpp", "*.cxx", "*.c++", "*.pcc",
    "*.ino", "*.hh", "*.hpp", "*.hxx", "*.inl", "*.ipp", "*.cs", "*.csx",
    "*.m", "*.mm", "*.java", "*.kt", "*.kts", "*.sc", "*.scala",
    "*.js", "*.cjs", "*.mjs", "*.ts", "*.cts", "*.mts", "*.jsx", "*.tsx",
    "*.coffee", "*.py", "*.pyw", "*.pyi", "*.rb", "*.pl", "*.plx", "*.pm",
    "*.php", "*.go", "*.rs", "*.swift", "*.dart", "*.lua", "*.r", "*.jl",
    "*.hs", "*.erl", "*.hrl", "*.ex", "*.exs", "*.clj", "*.cljc",
    "*.zig", "*.nim", "*.s", "*.asm", "*.d", "*.ml", "*.mli",
    "*.f03", "*.f08", "*.f90", "*.f95", "*.f", "*.for", "*.ftn", "*.f77",
    "*.groovy", "*.grt", "*.gtpl", "*.gvy", "*.sol", "*.gd", "*.vue",
    "*.ps1", "*.psm1", "*.sh",
]


def _run_git(args: list[str], timeout: int, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, killing the entire process group on timeout."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # own process group
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)  # SIGKILL entire group
        proc.wait()
        raise


def _check(
    result: subprocess.CompletedProcess, what: str,
) -> subprocess.CompletedProcess:
    """Raise ``RuntimeError`` if a git step failed.

    A non-zero git exit must not be silently ignored: a failed sparse-clone
    leaves an empty working tree that downstream tools (scc, semgrep) would
    measure as a genuine empty repo — recording a fake 0-LOC snapshot.
    """
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"{what} failed (rc={result.returncode}): {err}")
    return result


def resolve_mainline_sha(
    repo: str, branch: str, pinned_sha: str, before: str | None = None,
    *, repo_url: str | None = None, timeout: int = 120,
) -> tuple[str, bool]:
    """Resolve the snapshot SHA to analyse, constrained to the default
    branch's **first-parent (mainline) history**.

    GitHub's Commits API (``/commits?since=&until=``) returns the newest
    commit *by date* reachable from a branch — which includes commits
    merged in from side branches. A repo that periodically merges a shared
    CI-template branch (e.g. epage's ``template-update``) can have a tiny
    ~25-file template commit as its newest dated commit; pinning a code
    analysis to it measures the template, not the repo (toml-rs/toml → 11
    LOC). Such a commit is a genuine *ancestor* of the branch, so a plain
    ``merge-base --is-ancestor`` check passes it — only first-parent
    membership distinguishes mainline from merged-in side branches.

    - If ``pinned_sha`` is on ``branch``'s first-parent line → return it.
    - Otherwise recompute: the newest first-parent commit at or before
      ``before`` (an ISO-8601 timestamp; the branch tip when omitted).

    ``repo_url`` overrides the GitHub URL (used by tests). Returns
    ``(sha, corrected)`` — ``corrected`` is True when the pinned SHA was
    off-mainline and a replacement was computed.

    Raises ``RuntimeError`` if ``branch`` cannot be fetched or has no
    first-parent commit in range — callers degrade to the pinned SHA.
    """
    url = repo_url or f"https://github.com/{repo}.git"
    with tempfile.TemporaryDirectory(prefix=f"{CLONE_TMP_PREFIX}mainline-") as tmp:
        _run_git(["git", "init", "--quiet", "."], timeout=10, cwd=tmp)
        _run_git(["git", "remote", "add", "origin", url], timeout=10, cwd=tmp)
        # Commit graph only (--filter=tree:0): no trees, no blobs — tiny
        # even for huge repos, and enough to walk first-parent history.
        fetched = _run_git(
            ["git", "fetch", "--quiet", "--no-tags", "--filter=tree:0", "origin", branch],
            timeout=timeout, cwd=tmp,
        )
        if fetched.returncode != 0:
            raise RuntimeError(
                f"cannot fetch {branch!r}: "
                f"{fetched.stderr.decode('utf-8', 'replace').strip()[:160]}"
            )
        first_parent = _run_git(
            ["git", "rev-list", "--first-parent", "FETCH_HEAD"],
            timeout=min(timeout, 60), cwd=tmp,
        )
        mainline = set(first_parent.stdout.decode().split())
        if pinned_sha and pinned_sha in mainline:
            return pinned_sha, False
        # Off-mainline (a merged side-branch commit) — recompute the newest
        # first-parent commit at or before the year-end cutoff.
        args = ["git", "rev-list", "-1", "--first-parent"]
        if before:
            args.append(f"--before={before}")
        args.append("FETCH_HEAD")
        recomputed = _run_git(args, timeout=min(timeout, 60), cwd=tmp)
        sha = recomputed.stdout.decode().strip()
        if not sha:
            raise RuntimeError(f"no first-parent commit on {branch!r} before {before}")
        return sha, True


def corrected_clone_sha(
    repo: str, branch: str | None, pinned_sha: str,
    cutoff: str | None = None, *, what: str = "",
) -> str:
    """The SHA to actually check out for a snapshot, correcting off-mainline pins.

    GitHub's Commits API — and fork-network object sharing — can pin a
    snapshot to a commit that is *not* on the default branch's first-parent
    line. The worst case is a shared CI/template commit that several
    unrelated repos all merge: one such commit (``4253271e…``, "chore(ci):
    Attempt to auto-update pre-commit") is the newest-by-date 2025 commit for
    five of epage's Rust repos (clap, toml, anstyle, env_logger, snapbox) and
    a sibling one for five jaraco/pypa Python repos. Checking it out measures
    the template (≈0 functions), not the repo.

    ``resolve_mainline_sha`` detects this and returns the real mainline commit
    at/before ``cutoff``. This wrapper applies it, logging a correction, and
    degrades to ``pinned_sha`` when the branch is unknown or unfetchable.

    Callers must still RECORD results under ``pinned_sha`` (the
    ``commits-years.csv`` join key) — only the *checkout* is corrected.
    ``what`` is a short tag (e.g. "lizard") for the log line.
    """
    if not branch or not pinned_sha:
        return pinned_sha
    tag = f"{what}: " if what else ""
    try:
        effective, corrected = resolve_mainline_sha(repo, branch, pinned_sha, cutoff)
    except Exception as e:
        log.warning("%s%s mainline check unavailable (%s) — using pinned SHA",
                    tag, repo, e)
        return pinned_sha
    if corrected:
        log.warning("%s%s pinned SHA %s is off-mainline (merged side branch) "
                    "— measuring mainline commit %s instead",
                    tag, repo, pinned_sha[:12], effective[:12])
    return effective


def download_tarball(
    repo: str, dest: str, client: httpx.Client, ref: str = "HEAD",
) -> tuple[float, int]:
    """Download and extract repo tarball via codeload. Returns (elapsed_s, size_bytes)."""
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    t0 = time.monotonic()
    # Retry network errors only
    last_err: Exception | None = None
    buf: io.BytesIO | None = None
    for attempt in range(3):
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                resp.raise_for_status()
                buf = io.BytesIO()
                for chunk in resp.iter_bytes(65536):
                    buf.write(chunk)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    else:
        raise last_err
    size = buf.tell()
    buf.seek(0)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar:
            try:
                tar.extract(member, dest, filter="data")
            except (IsADirectoryError, NotADirectoryError, PermissionError):
                continue
    return time.monotonic() - t0, size


def sparse_clone(
    repo: str, dest: str, size_kb: int = 0, ref: str | None = None,
    *, repo_url: str | None = None,
) -> tuple[float, int]:
    """Sparse checkout: clone metadata only, then fetch source code files. Returns (elapsed_s, approx_size_bytes).

    Raises ``RuntimeError`` if any git step fails — a failed fetch/checkout
    must not be mistaken for an empty repo. ``repo_url`` overrides the
    GitHub URL (used by tests).
    """
    url = repo_url or f"https://github.com/{repo}.git"
    # Scale timeout: 60s base + 60s per 100MB of repo size
    clone_timeout = 60 + (size_kb // 100_000) * 60
    t0 = time.monotonic()
    if ref:
        # Fetch a specific commit by SHA — combine init+remote+sparse into one shell call
        os.makedirs(dest, exist_ok=True)
        exts_args = " ".join(f"'{e}'" for e in SOURCE_EXTS)
        init_script = (
            f"git init --quiet . && "
            f"git remote add origin '{url}' && "
            f"git sparse-checkout set --no-cone {exts_args}"
        )
        _check(_run_git(["sh", "-c", init_script], timeout=10, cwd=dest),
               f"{repo}: sparse-checkout init")
        _check(
            _run_git(
                ["git", "fetch", "--depth", "1", "--quiet", "--no-tags",
                 "--filter=blob:none", "origin", ref],
                timeout=clone_timeout, cwd=dest,
            ),
            f"{repo}: fetch {ref[:12]}",
        )
        _check(
            _run_git(
                ["git", "checkout", "--quiet", "FETCH_HEAD"],
                timeout=clone_timeout, cwd=dest,
            ),
            f"{repo}: checkout",
        )
    else:
        _check(
            _run_git(
                ["git", "clone", "--depth", "1", "--quiet", "--no-tags",
                 "--filter=blob:none", "--sparse", url, dest],
                timeout=clone_timeout,
            ),
            f"{repo}: clone",
        )
        _check(
            _run_git(
                ["git", "sparse-checkout", "set", "--no-cone"] + SOURCE_EXTS,
                timeout=clone_timeout, cwd=dest,
            ),
            f"{repo}: sparse-checkout set",
        )
    elapsed = time.monotonic() - t0
    size = 0
    git_objects = os.path.join(dest, ".git", "objects", "pack")
    if os.path.isdir(git_objects):
        for f in os.listdir(git_objects):
            if f.endswith(".pack"):
                size += os.path.getsize(os.path.join(git_objects, f))
    return elapsed, size


def bare_blobless_clone(
    repo: str, dest: str, ref: str | None = None,
    *, repo_url: str | None = None,
) -> tuple[float, int]:
    """Bare blobless clone: full commit graph, lazy blob fetch.

    Used by history-walking tools (gitpandas, truckfactor) that need
    every commit object but not file contents. If `ref` is given,
    fetches it into a local branch `_pinned` so callers can analyze a
    specific commit deterministically.

    ``repo_url`` overrides the clone URL so a non-GitHub repo (gitlab, …)
    clones its real host instead of the assumed ``github.com/{repo}``.

    Returns (elapsed_s, on_disk_bytes_after_clone).
    """
    url = repo_url or f"https://github.com/{repo}.git"
    clone_timeout = 300  # bare blobless clones are smaller than sparse but full graph
    t0 = time.monotonic()
    _run_git(
        ["git", "clone", "--bare", "--filter=blob:none", "--no-tags",
         "--quiet", url, dest],
        timeout=clone_timeout,
    )
    if ref:
        _run_git(
            ["git", "-C", dest, "fetch", "--quiet", "--no-tags",
             "origin", f"{ref}:refs/heads/_pinned"],
            timeout=clone_timeout,
        )
    elapsed = time.monotonic() - t0
    size = 0
    for root, _dirs, files in os.walk(dest):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return elapsed, size


def bare_treeless_clone(
    repo: str, dest: str, timeout: int = 300, *, repo_url: str | None = None,
) -> tuple[float, int]:
    """Bare clone with ``--filter=tree:0 --no-tags`` — commit graph only.

    The lightest possible clone: every commit object, but no trees and no
    blobs. Enough for author/date history walking — ``git log --pretty``,
    ``git shortlog``, ``git rev-list`` — which is all the git-based
    contributor fetcher (``src.sources.git.contributors``) needs. Far smaller and
    faster than ``bare_blobless_clone`` on big repos, which still pulls
    every tree object. ``git log`` without ``-p`` / ``--stat`` /
    ``--name-*`` never dereferences a tree, so no lazy fetch is triggered.

    ``repo_url`` overrides the clone URL so a non-GitHub repo (gitlab, …)
    clones its real host instead of the assumed ``github.com/{repo}``.

    Raises ``RuntimeError`` (via ``_check``) on a non-zero clone exit
    (404 / private / network), so batch callers can record the failure
    per repo. ``subprocess.TimeoutExpired`` propagates on timeout.

    Returns ``(elapsed_s, on_disk_bytes)``.
    """
    url = repo_url or f"https://github.com/{repo}.git"
    t0 = time.monotonic()
    _check(
        _run_git(
            ["git", "clone", "--bare", "--filter=tree:0", "--no-tags",
             "--quiet", url, dest],
            timeout=timeout,
        ),
        f"{repo}: treeless clone",
    )
    elapsed = time.monotonic() - t0
    size = 0
    for root, _dirs, files in os.walk(dest):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return elapsed, size


def fetch_all_blobs(dest: str, timeout: int = 600) -> tuple[float, int]:
    """Bulk-fetch all blobs into an existing bare blobless clone.

    Required before running ``git blame`` at scale on a blobless clone:
    each blame call would otherwise lazy-fetch ancestor blobs one at a
    time over the network (one round-trip per missing blob — the actual
    bottleneck behind the original gitpandas-based fetcher).

    Implementation: removes the partial-clone filter from the remote
    config, then fetches with ``--refetch`` to bring in every blob
    reachable from the existing refs. After this the clone is no longer
    blobless — typical size grows ~2-10× from blobless. Caller owns
    cleanup of the on-disk clone afterwards.

    Returns (elapsed_s, on_disk_bytes_after_fetch).
    """
    t0 = time.monotonic()
    # Drop the partial filter so the refetch pulls full blobs. ``|| true``
    # in Python form: ignore exit codes from `--unset` since the keys may
    # not exist on every clone configuration.
    for key in ("remote.origin.partialclonefilter", "remote.origin.promisor"):
        try:
            subprocess.run(
                ["git", "-C", dest, "config", "--unset", key],
                capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
    _run_git(
        ["git", "-C", dest, "fetch", "--quiet", "--no-tags", "--refetch", "origin"],
        timeout=timeout,
    )
    elapsed = time.monotonic() - t0
    size = 0
    for root, _dirs, files in os.walk(dest):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return elapsed, size
