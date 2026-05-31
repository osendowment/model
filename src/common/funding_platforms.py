"""Canonical funding platforms + URL→platform detection.

Shared by the FUNDING.yml fetcher (`src.sources.github.fetch_funding_yml`), the FLOSS
Fund export parser (`src.sources.floss_fund.funding_json`), and the funding builder
(`src.risk.build_funding`). FUNDING.yml uses these keys directly;
funding.json declares free-form channel `address` URLs, which we map back to
the same platform vocabulary so the two sources can be unioned.
"""
from __future__ import annotations

import re

# GitHub's documented FUNDING.yml platforms (+ thanks_dev, seen in our data).
# `custom` is the catch-all for arbitrary URLs. Order = CSV column order.
FUNDING_PLATFORMS = [
    "github", "patreon", "open_collective", "ko_fi", "tidelift",
    "community_bridge", "liberapay", "issuehunt", "lfx_crowdfunding",
    "polar", "buy_me_a_coffee", "thanks_dev", "otechie", "custom",
]

# host substring → canonical platform. First match wins.
_HOST_PLATFORM = [
    ("github.com/sponsors", "github"),
    ("patreon.com", "patreon"),
    ("opencollective.com", "open_collective"),
    ("ko-fi.com", "ko_fi"),
    ("tidelift.com", "tidelift"),
    ("liberapay.com", "liberapay"),
    ("issuehunt.io", "issuehunt"),
    ("polar.sh", "polar"),
    ("buymeacoffee.com", "buy_me_a_coffee"),
    ("buymeacoff.ee", "buy_me_a_coffee"),
    ("thanks.dev", "thanks_dev"),
    ("otechie.com", "otechie"),
]


def platform_handle_from_url(url: str | None) -> tuple[str | None, str]:
    """Map a funding URL to (canonical_platform, handle).

    Known hosts return their platform and the trailing slug
    (`opencollective.com/vuejs` → `("open_collective", "vuejs")`). An
    unrecognised URL returns `(None, <original url>)` so the caller can file
    it under `custom`. Blank input returns `(None, "")`.
    """
    u = (url or "").strip()
    if not u:
        return None, ""
    low = u.lower()
    for host, platform in _HOST_PLATFORM:
        if host in low:
            slug = low.split(host, 1)[1].lstrip("/")
            slug = re.split(r"[/?#]", slug, maxsplit=1)[0]
            return platform, slug or u
    return None, u


def normalize_oc_slug(value: str | None) -> str:
    """An Open Collective handle/URL → bare lowercased slug.

    `https://opencollective.com/Babel` → `babel`; `babel` → `babel`.
    """
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "opencollective.com" in v:
        v = v.split("opencollective.com", 1)[1].lstrip("/")
    return re.split(r"[/?#]", v, maxsplit=1)[0]
