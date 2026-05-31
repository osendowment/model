#!/usr/bin/env python3
"""Independent Claude-knowledge guesses for AB github/git gaps.

V2: revised after HTTP-verifying every candidate slug and cross-checking
against the LLM-finder cache (`claude-repo-data.csv`). Every entry below
has been deliberately reviewed; reasoning calls out where my V1 guess was
wrong, where the LLM's answer beat mine, and where I'm holding firm.

Schema mirrors `claude-repo-data.csv`. Confidence reflects post-verification
honesty, not pre-verification gut-feel.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INPUT = DATA_DIR / "value" / "value.csv"
OUTPUT = DATA_DIR / "sources" / "llms" / "claude-git-guess.csv"

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
MODEL = "claude-opus-4-7-training-knowledge+http-verify"

FIELDS = [
    "package", "ecosystem", "value_class",
    "github_repo", "canonical_git_url", "gh_mirror",
    "confidence", "self_confidence_raw", "verified",
    "agreement", "n_runs", "dissent",
    "tier", "source", "reasoning", "review_note",
    "checked_at", "model",
]

# (github_repo, canonical_git_url_override, confidence, reasoning)
GUESSES: dict[tuple[str, str], tuple[str, str, float, str]] = {

    # ── cpp ──────────────────────────────────────────────────────────────────

    ("acl", "cpp"): ("", "", 0.40,
        "GNU acl on Savannah. LLM finder picked qunixorg/acl, which is a "
        "packaging fork rather than a canonical mirror — leaving empty."),
    ("aom", "cpp"): ("", "", 0.70,
        "AV1 reference encoder at aomedia.googlesource.com. LLM finder picked "
        "aliveteam/aom at 0.42 conf — not authoritative. No widely-known mirror."),
    ("autoconf", "cpp"): ("autotools-mirror/autoconf", "", 0.95,
        "HTTP-verified. autotools-mirror is the conventional GNU autotools "
        "mirror org. Canonical: git.savannah.gnu.org/cgit/autoconf.git."),
    ("bzip2", "cpp"): ("", "", 0.40,
        "Canonical at sourceware.org/bzip2. No widely-known canonical GH mirror."),
    ("cairo-graphics-library", "cpp"): ("freedesktop-unofficial-mirror/cairo", "", 0.70,
        "freedesktop-unofficial-mirror is a recognised mirror org for fdo "
        "projects (verified). Canonical: gitlab.freedesktop.org/cairo/cairo."),
    ("dbus", "cpp"): ("", "", 0.40,
        "LLM picked d-bus/dbus but the canonical fdo project does not have "
        "a definitive GH mirror I can confidently identify."),
    ("fontconfig", "cpp"): ("fontconfig/fontconfig", "", 0.85,
        "HTTP-verified. github.com/fontconfig/fontconfig is the project's "
        "official-org GH mirror of gitlab.freedesktop.org/fontconfig/fontconfig."),
    ("gdbm", "cpp"): ("", "", 0.40,
        "GNU gdbm at git.gnu.org.ua. LLM picked distrotech/gdbm — a packaging "
        "org, not authoritative. Leaving empty."),
    ("gdk-pixbuf", "cpp"): ("GNOME/gdk-pixbuf", "", 0.85,
        "HTTP-verified. GNOME/* on GitHub auto-mirrors gitlab.gnome.org/GNOME/*. "
        "Canonical: gitlab.gnome.org/GNOME/gdk-pixbuf."),
    ("giflib", "cpp"): ("", "", 0.30,
        "Canonical at sourceforge git. LLM picked nesbox/giflib (personal, "
        "low conf). Leaving empty."),
    ("glib", "cpp"): ("GNOME/glib", "", 0.90,
        "HTTP-verified. GNOME's official GitHub auto-mirror. Canonical: "
        "gitlab.gnome.org/GNOME/glib."),
    ("glibc", "cpp"): ("bminor/glibc", "", 0.90,
        "HTTP-verified. Roland McGrath / Carlos O'Donell maintain glibc; "
        "bminor/glibc is the well-known unofficial mirror updated daily from "
        "sourceware. Canonical: sourceware.org/git/glibc.git."),
    ("gmp", "cpp"): ("gmp-mirror/gmp", "https://gmplib.org/repo/gmp/", 0.65,
        "HTTP-verified. gmp-mirror/gmp follows the autotools-mirror naming "
        "convention. Canonical is gmplib.org Mercurial — unusual for this "
        "table; included for completeness."),
    ("gnumpc", "cpp"): ("", "", 0.30,
        "GNU MPC at gitlab.inria.fr/mpc/mpc. No widely-known GH mirror."),
    ("gnutls", "cpp"): ("gnutls/gnutls", "", 0.85,
        "HTTP-verified. gnutls org owns its mirror on GitHub. Canonical: "
        "gitlab.com/gnutls/gnutls."),
    ("isl", "cpp"): ("", "", 0.35,
        "isl at repo.or.cz. LLM picked serge-sans-paille/isl — personal "
        "account, not authoritative. Leaving empty."),
    ("libgpg-error", "cpp"): ("gpg/libgpg-error",
                              "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=libgpg-error.git", 0.65,
        "HTTP-verified. GnuPG project. Canonical at git.gnupg.org."),
    ("libidn2", "cpp"): ("libidn/libidn2", "", 0.85,
        "HTTP-verified. libidn org owns its mirror. Canonical: gitlab.com/libidn/libidn2."),
    ("libtasn1", "cpp"): ("gnutls/libtasn1", "", 0.85,
        "HTTP-verified. gnutls org also owns libtasn1 mirror. Canonical: "
        "gitlab.com/gnutls/libtasn1."),
    ("libtool", "cpp"): ("autotools-mirror/libtool", "", 0.92,
        "HTTP-verified. autotools-mirror org. Canonical: git.savannah.gnu.org/cgit/libtool.git."),
    ("libunistring", "cpp"): ("", "", 0.30,
        "GNU libunistring on Savannah. LLM picked gnosis/libunistring (personal, "
        "not authoritative). Leaving empty."),
    ("libx11", "cpp"): ("", "", 0.30,
        "X.Org core lib at gitlab.freedesktop.org/xorg/lib/libx11. LLM picked "
        "mirror/libx11 — vague org name; not confident."),
    ("libxau", "cpp"): ("", "", 0.30, "X.Org lib; no confident GH mirror."),
    ("libxcb", "cpp"): ("gitlab-freedesktop-mirrors/libxcb", "", 0.65,
        "HTTP-verified. fdo mirror org on GitHub."),
    ("libxdmcp", "cpp"): ("", "", 0.30, "X.Org lib; no confident GH mirror."),
    ("libxext", "cpp"): ("", "", 0.30, "X.Org lib; no confident GH mirror."),
    ("libxml2", "cpp"): ("GNOME/libxml2", "", 0.90,
        "HTTP-verified. GNOME GitHub auto-mirror. Canonical: gitlab.gnome.org/GNOME/libxml2."),
    ("libxrender", "cpp"): ("", "", 0.30, "X.Org lib; no confident GH mirror."),
    ("m4", "cpp"): ("autotools-mirror/m4", "", 0.92,
        "HTTP-verified. autotools-mirror org. Canonical: git.savannah.gnu.org/cgit/m4.git."),
    ("mpdecimal", "cpp"): ("", "https://www.bytereef.org/mpdecimal", 0.70,
        "libmpdec at bytereef.org (Mercurial). No GH mirror. URL is project "
        "home, not strictly Git."),
    ("mpfr", "cpp"): ("", "", 0.30,
        "GNU MPFR at gitlab.inria.fr/mpfr/mpfr. No widely-known GH mirror."),
    ("nettle", "cpp"): ("gnutls/nettle", "", 0.80,
        "HTTP-verified. gnutls org also mirrors nettle (Niels Möller's lib). "
        "Canonical: git.lysator.liu.se."),
    ("pixman", "cpp"): ("libpixman/pixman", "", 0.65,
        "HTTP-verified. libpixman is the org-mirror name. Canonical: "
        "gitlab.freedesktop.org/pixman/pixman."),
    ("python3-defaults", "cpp"): ("", "https://salsa.debian.org/cpython-team/python3-defaults.git", 0.85,
        "Pure Debian metapackage — no real upstream. Should be filtered out "
        "of cpp pipeline. Not a fundable project."),
    ("qt", "cpp"): ("qt/qt5", "", 0.92,
        "HTTP-verified. qt org on GitHub mirrors code.qt.io. qt/qt5 is the "
        "meta-repo; sub-modules at qt/qtbase, qt/qtdeclarative, etc."),
    ("readline", "cpp"): ("", "https://git.savannah.gnu.org/cgit/readline.git", 0.50,
        "V1 GUESS WRONG: I claimed bminor/readline by analogy to bminor/glibc "
        "and bminor/bash — HTTP HEAD returned 404. The pattern doesn't extend. "
        "LLM finder offers sailfishos-mirror/readline (verified) but that's a "
        "distro-packaging mirror, not authoritative. Leaving empty; canonical "
        "is Savannah."),
    ("tcp-wrappers", "cpp"): ("", "", 0.20,
        "Upstream officially dead since 1997. Salsa packaging is the only living "
        "artefact. NOT a fundable upstream."),
    ("tiff", "cpp"): ("", "", 0.40,
        "libtiff at gitlab.com/libtiff/libtiff. LLM picked libsdl-org/libtiff "
        "— a fork by the SDL team for game-engine usage, NOT the canonical "
        "mirror. Leaving empty."),

    # ── pypi ─────────────────────────────────────────────────────────────────

    ("acme", "pypi"): ("certbot/certbot", "https://github.com/certbot/certbot.git", 0.95,
        "HTTP-verified. EFF's `acme` PyPI package is part of the certbot "
        "mono-repo at certbot/certbot under the `acme/` subdirectory."),
    ("aioitertools", "pypi"): ("omnilib/aioitertools", "https://github.com/omnilib/aioitertools.git", 0.92,
        "HTTP-verified. John Reese's project; lives under the omnilib org."),
    ("anyio", "pypi"): ("agronholm/anyio", "https://github.com/agronholm/anyio.git", 0.97,
        "HTTP-verified. Alex Grönholm's async-compat library."),
    ("attrs", "pypi"): ("python-attrs/attrs", "https://github.com/python-attrs/attrs.git", 0.99,
        "HTTP-verified. Hynek Schlawack's attrs library; canonical at "
        "python-attrs/attrs. NOTE: LLM finder incorrectly picked sponsors/hynek "
        "(GitHub Sponsors page, not a code repo) — its registry-API fast path "
        "grabbed the first github URL from PyPI project_urls without filtering "
        "out funding links. My answer is the actual code repo."),
    ("awscli", "pypi"): ("aws/aws-cli", "https://github.com/aws/aws-cli.git", 0.99,
        "HTTP-verified. AWS Command Line Interface."),
    ("azure-common", "pypi"): ("Azure/azure-sdk-for-python", "https://github.com/Azure/azure-sdk-for-python.git", 0.95,
        "HTTP-verified. Azure SDK mono-repo at Azure/azure-sdk-for-python."),
    ("azure-core", "pypi"): ("Azure/azure-sdk-for-python", "https://github.com/Azure/azure-sdk-for-python.git", 0.97, "Same Azure mono-repo."),
    ("azure-mgmt-core", "pypi"): ("Azure/azure-sdk-for-python", "https://github.com/Azure/azure-sdk-for-python.git", 0.95, "Same Azure mono-repo."),
    ("azure-nspkg", "pypi"): ("Azure/azure-sdk-for-python", "https://github.com/Azure/azure-sdk-for-python.git", 0.85,
        "Namespace package; part of the Azure mono-repo."),
    ("azure-storage-blob", "pypi"): ("Azure/azure-sdk-for-python", "https://github.com/Azure/azure-sdk-for-python.git", 0.97, "Same Azure mono-repo."),
    ("backports-zstd", "pypi"): ("rogdham/backports.zstd", "https://github.com/rogdham/backports.zstd.git", 0.70,
        "HTTP-verified. Romain Dechazal-Pouey's backport of Python 3.14+ zstd "
        "stdlib. Confidence boosted from V1 0.20 to 0.70 after verification."),
    ("beautifulsoup4", "pypi"): ("", "https://code.launchpad.net/beautifulsoup", 0.65,
        "Canonical on Launchpad (Bazaar VCS). LLM picked waylan/beautifulsoup "
        "at 0.05 conf — not authoritative."),
    ("botocore", "pypi"): ("boto/botocore", "https://github.com/boto/botocore.git", 0.99,
        "HTTP-verified. AWS SDK low-level core."),
    ("certbot-dns-cloudflare", "pypi"): ("certbot/certbot", "https://github.com/certbot/certbot.git", 0.95,
        "HTTP-verified. Part of the certbot mono-repo."),
    ("decorator", "pypi"): ("micheles/decorator", "https://github.com/micheles/decorator.git", 0.92,
        "HTTP-verified. Michele Simionato's decorator library."),
    ("docutils", "pypi"): ("", "", 0.30,
        "Canonical at SourceForge SVN. LLM picked docutils/docutils-sourceforge-backup "
        "— suspicious naming suggests a one-off backup, not a canonical mirror."),
    ("et-xmlfile", "pypi"): ("", "https://foss.heptapod.net/openpyxl/et_xmlfile.git", 0.70,
        "openpyxl sub-project on Heptapod (Mercurial). No GH mirror."),
    ("exceptiongroup", "pypi"): ("agronholm/exceptiongroup", "https://github.com/agronholm/exceptiongroup.git", 0.95,
        "HTTP-verified. Alex Grönholm's PEP 654 backport."),
    ("filelock", "pypi"): ("tox-dev/filelock", "https://github.com/tox-dev/filelock.git", 0.95,
        "HTTP-verified. Maintained under tox-dev org."),
    ("google-api-core", "pypi"): ("googleapis/python-api-core", "https://github.com/googleapis/python-api-core.git", 0.85,
        "HTTP-verified. Both googleapis/python-api-core and googleapis/google-cloud-python "
        "exist; LLM picked the mono-repo, but python-api-core is the dedicated "
        "source. Holding with the more-specific one."),
    ("google-auth-oauthlib", "pypi"): ("googleapis/google-auth-library-python-oauthlib",
        "https://github.com/googleapis/google-auth-library-python-oauthlib.git", 0.85,
        "HTTP-verified. Dedicated repo for the oauthlib companion to google-auth."),
    ("googleapis-common-protos", "pypi"): ("googleapis/api-common-protos",
        "https://github.com/googleapis/api-common-protos.git", 0.65,
        "HTTP-verified. .proto definitions live here; the auto-generated Python "
        "package may live in google-cloud-python mono-repo (LLM's pick). Holding "
        "with the .proto source — both are arguably canonical."),
    ("grpc-google-iam-v1", "pypi"): ("googleapis/python-grpc-google-iam-v1",
        "https://github.com/googleapis/python-grpc-google-iam-v1.git", 0.65,
        "HTTP-verified. Dedicated googleapis repo. LLM picked the umbrella "
        "google-cloud-python — both exist."),
    ("grpcio", "pypi"): ("grpc/grpc", "https://github.com/grpc/grpc.git", 0.97,
        "HTTP-verified. Python bindings live in grpc/grpc mono-repo under "
        "src/python."),
    ("grpcio-status", "pypi"): ("grpc/grpc", "https://github.com/grpc/grpc.git", 0.92, "Same grpc/grpc mono-repo."),
    ("grpcio-tools", "pypi"): ("grpc/grpc", "https://github.com/grpc/grpc.git", 0.92, "Same grpc/grpc mono-repo."),
    ("jaraco-develop", "pypi"): ("jaraco/jaraco.develop", "https://github.com/jaraco/jaraco.develop.git", 0.95,
        "HTTP-verified. V1 GUESS WRONG: I guessed jaraco/develop but that 404s. "
        "Jason R. Coombs uses the dot-naming convention `jaraco.foo`."),
    ("jeepney", "pypi"): ("", "https://gitlab.com/takluyver/jeepney.git", 0.55,
        "Thomas Kluyver's pure-Python D-Bus library. Canonical on gitlab.com. "
        "No widely-known GH mirror."),
    ("jmespath", "pypi"): ("jmespath/jmespath.py", "https://github.com/jmespath/jmespath.py.git", 0.97,
        "HTTP-verified. JMESPath spec implementation in Python."),
    ("openpyxl", "pypi"): ("", "https://foss.heptapod.net/openpyxl/openpyxl", 0.70,
        "Canonical on Heptapod (Mercurial). No widely-known GH mirror."),
    ("opentelemetry-sdk", "pypi"): ("open-telemetry/opentelemetry-python", "https://github.com/open-telemetry/opentelemetry-python.git", 0.97,
        "HTTP-verified. Official OTel Python SDK."),
    ("pluggy", "pypi"): ("pytest-dev/pluggy", "https://github.com/pytest-dev/pluggy.git", 0.97,
        "HTTP-verified. pytest's plugin-management system."),
    ("protobuf", "pypi"): ("protocolbuffers/protobuf", "https://github.com/protocolbuffers/protobuf.git", 0.99,
        "HTTP-verified. Google's Protocol Buffers; renamed from google/protobuf "
        "to protocolbuffers/protobuf years ago."),
    ("py", "pypi"): ("pytest-dev/py", "https://github.com/pytest-dev/py.git", 0.92,
        "HTTP-verified. Deprecated `py` library that pytest historically used."),
    ("py4j", "pypi"): ("py4j/py4j", "https://github.com/py4j/py4j.git", 0.95,
        "HTTP-verified. V1 GUESS UPDATED: I guessed bartdag/py4j (Barthélémy "
        "Dagenais's personal account); that URL still works but redirects to "
        "py4j/py4j (org-named canonical)."),
    ("pytest-perf", "pypi"): ("jaraco/pytest-perf", "https://github.com/jaraco/pytest-perf.git", 0.92,
        "HTTP-verified. Jason R. Coombs's pytest performance plugin."),
    ("pytest-ruff", "pypi"): ("businho/pytest-ruff", "https://github.com/businho/pytest-ruff.git", 0.80,
        "HTTP-verified. Confidence boosted from V1 0.55 to 0.80."),
    ("pytz", "pypi"): ("stub42/pytz", "https://github.com/stub42/pytz.git", 0.95,
        "HTTP-verified. Stuart Bishop's IANA TZ database wrapper."),
    ("requests", "pypi"): ("psf/requests", "https://github.com/psf/requests.git", 0.99,
        "HTTP-verified. Kenneth Reitz's HTTP library; moved from kennethreitz/ "
        "to psf/."),
    ("ruamel-yaml-clib", "pypi"): ("", "", 0.30,
        "Anthon van der Neut's lib; canonical on SourceForge. LLM picked "
        "ruamel/yaml.clib but I'm not confident the ruamel org on GitHub is "
        "the canonical mirror."),
    ("sortedcontainers", "pypi"): ("grantjenks/python-sortedcontainers", "https://github.com/grantjenks/python-sortedcontainers.git", 0.95,
        "HTTP-verified. Grant Jenks's sorted collections."),
    ("sqlalchemy", "pypi"): ("sqlalchemy/sqlalchemy", "https://github.com/sqlalchemy/sqlalchemy.git", 0.99,
        "HTTP-verified. Mike Bayer's SQLAlchemy ORM."),
    ("tomlkit", "pypi"): ("python-poetry/tomlkit", "https://github.com/python-poetry/tomlkit.git", 0.95,
        "HTTP-verified. Sébastien Eustace's TOML library, used by Poetry."),
    ("typing-inspection", "pypi"): ("pydantic/typing-inspection", "https://github.com/pydantic/typing-inspection.git", 0.85,
        "HTTP-verified. Pydantic's typing helpers."),
    ("webencodings", "pypi"): ("gsnedders/python-webencodings", "https://github.com/gsnedders/python-webencodings.git", 0.80,
        "HTTP-verified. Geoffrey Sneddon's encodings library. Confidence boosted "
        "from V1 0.40 to 0.80."),
    ("wmi", "pypi"): ("tjguk/wmi", "https://github.com/tjguk/wmi.git", 0.92,
        "HTTP-verified. Tim Golden's Windows WMI wrapper."),
}


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rows_in = list(csv.DictReader(open(INPUT, encoding="utf-8")))
    ab_gaps = [
        r for r in rows_in
        if r["class"] in ("A", "B")
        and (not r["github_repo"].strip() or not r["git_url"].strip())
    ]

    out_rows: list[dict] = []
    missing: list[tuple[str, str]] = []
    for r in ab_gaps:
        pkg, eco = r["top_eco_pkg"], r["top_eco"]
        existing_canon = r["git_url"].strip()
        if (pkg, eco) not in GUESSES:
            missing.append((pkg, eco))
            continue
        gh_repo, canon_override, conf, reason = GUESSES[(pkg, eco)]
        canon = canon_override or existing_canon
        if not canon and gh_repo:
            canon = f"https://github.com/{gh_repo}.git"
        host = ""
        if canon:
            try:
                host = (urlparse(canon).hostname or "").lower()
            except Exception:
                pass
        gh_mirror = bool(host) and host != "github.com"
        out_rows.append({
            "package": pkg,
            "ecosystem": eco,
            "value_class": r["class"],
            "github_repo": gh_repo,
            "canonical_git_url": canon,
            "gh_mirror": str(gh_mirror),
            "confidence": f"{conf:.2f}",
            "self_confidence_raw": f"{conf:.2f}",
            "verified": "true" if gh_repo else "",
            "agreement": "claude-knowledge+http",
            "n_runs": "1",
            "dissent": "",
            "tier": "claude-guess",
            "source": "training data + HTTP HEAD",
            "reasoning": reason,
            "review_note": "",
            "checked_at": NOW,
            "model": MODEL,
        })

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Wrote {len(out_rows)} guess rows → {OUTPUT}")
    if missing:
        print(f"\n[!] {len(missing)} AB gaps in value-data but not in GUESSES dict:")
        for pkg, eco in missing:
            print(f"   {eco}/{pkg}")


if __name__ == "__main__":
    main()
