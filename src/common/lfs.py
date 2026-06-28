"""Git LFS helpers — tell materialised data apart from unfetched pointers.

When the repo is configured for lazy LFS (``git lfs install --skip-smudge`` +
``lfs.fetchexclude``), large tracked files sit on disk as small *pointer* files
instead of the real data. A pointer looks like::

    version https://git-lfs.github.com/spec/v1
    oid sha256:<64 hex>
    size <bytes>

Fetchers must treat such a pointer as "not present" so they re-fetch on demand
(``has_real_data``), and readers should fail with a clear message rather than
trying to parse the pointer text as CSV (``is_lfs_pointer``).
"""
from __future__ import annotations

from pathlib import Path

_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def is_lfs_pointer(path: str | Path) -> bool:
    """True if ``path`` is an unmaterialised Git LFS pointer.

    Pointer files are tiny and start with the LFS spec marker; the real data
    lives on the LFS remote. Returns False for missing files, directories, or
    genuine data.
    """
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size > 1024:
            return False
        with p.open("rb") as f:
            return f.read(len(_LFS_MAGIC)) == _LFS_MAGIC
    except OSError:
        return False


def has_real_data(path: str | Path) -> bool:
    """True if ``path`` exists as materialised data — present and not a pointer.

    Use for "do we already have this input?" checks so an LFS pointer counts as
    missing and triggers a re-fetch instead of a false "already present".
    """
    return Path(path).is_file() and not is_lfs_pointer(path)
