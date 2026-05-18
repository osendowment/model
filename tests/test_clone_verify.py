"""Regression test for first-parent snapshot resolution (src/git/clone.py).

Reproduces the real toml-rs/toml bug: GitHub's Commits API returned a
merged CI-template commit — reachable from the default branch but off its
first-parent (mainline) line — as the "last 2025 commit", so scc measured
a 25-file template tree → 11 LOC. ``resolve_mainline_sha`` must detect the
off-mainline SHA and recompute the real last first-parent commit.
"""

import os
import subprocess

from src.git.clone import resolve_mainline_sha


def _run(cwd, *args, env=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, **(env or {})},
    ).stdout.strip()


def _commit(repo, name, date) -> str:
    (repo / name).write_text(name + "\n")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name,
         env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return _run(repo, "rev-parse", "HEAD")


def test_resolve_mainline_sha(tmp_path):
    # `main` has A, B; a `side` branch off A adds S (dated *later* than B);
    # `side` is then merged into `main` as merge commit M. So main's
    # first-parent line is [M, B, A] — S is reachable but off-mainline.
    remote = tmp_path / "remote"
    remote.mkdir()
    _run(remote, "-c", "init.defaultBranch=main", "init", "-q")
    sha_a = _commit(remote, "a.txt", "2024-01-01T00:00:00")
    sha_b = _commit(remote, "b.txt", "2025-06-01T00:00:00")
    _run(remote, "checkout", "-q", "-b", "side", sha_a)
    sha_s = _commit(remote, "s.txt", "2025-12-31T00:00:00")
    _run(remote, "checkout", "-q", "main")
    _run(remote, "-c", "user.email=t@t", "-c", "user.name=t",
         "merge", "--no-ff", "-m", "merge side", "side",
         env={"GIT_AUTHOR_DATE": "2026-02-01T00:00:00",
              "GIT_COMMITTER_DATE": "2026-02-01T00:00:00"})
    sha_m = _run(remote, "rev-parse", "HEAD")
    url = str(remote)

    # The merged side-branch commit S is off the first-parent line →
    # recompute. Newest first-parent commit at or before 2026-01-01 is B
    # (not S, not the 2026-dated merge M) — exactly the toml-rs/toml case.
    assert resolve_mainline_sha(
        "x/y", "main", sha_s, "2026-01-01T00:00:00", repo_url=url,
    ) == (sha_b, True)

    # With no cutoff the recompute returns the latest first-parent commit
    # (the merge M) — never the off-mainline side-branch commit S.
    assert resolve_mainline_sha("x/y", "main", sha_s, repo_url=url) == (sha_m, True)

    # A SHA already on the first-parent line is returned untouched.
    assert resolve_mainline_sha("x/y", "main", sha_b, repo_url=url) == (sha_b, False)
    assert resolve_mainline_sha("x/y", "main", sha_m, repo_url=url) == (sha_m, False)
