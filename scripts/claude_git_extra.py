#!/usr/bin/env python3
"""V3 expansion: hand-curated github/git URLs for ABC empty-git rows.

Reads the existing `claude-git-data.csv`, applies the EXTRAS dict below, then
writes back. Each entry includes a one-line reason + my honest confidence.
HTTP-verified separately (see the caller).
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TARGET = DATA_DIR / "sources" / "llms" / "claude-git-data.csv"

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

# (package, ecosystem) → (github_repo, canonical_git_url, conf, reason)
# Empty github_repo means the project has no canonical GitHub presence.
EXTRAS: dict[tuple[str, str], tuple[str, str, float, str]] = {

    # ── pypi: well-known projects ────────────────────────────────────────────
    ("aiodns", "pypi"): ("saghul/aiodns", "", 0.92, "Saúl Ibarra Corretgé's async DNS resolver."),
    ("annotated-doc", "pypi"): ("", "", 0.30, "Could not confidently identify."),
    ("antlr4-python3-runtime", "pypi"): ("antlr/antlr4", "", 0.95, "ANTLR mono-repo."),
    ("apache-airflow", "pypi"): ("apache/airflow", "", 0.99, "Apache Airflow."),
    ("apache-airflow-core", "pypi"): ("apache/airflow", "", 0.95, "Same airflow mono-repo."),
    ("apache-airflow-providers-apache-cassandra", "pypi"): ("apache/airflow", "", 0.92, "Provider package in airflow mono-repo."),
    ("apache-airflow-providers-common-compat", "pypi"): ("apache/airflow", "", 0.92, "Provider package in airflow mono-repo."),
    ("apache-airflow-providers-common-sql", "pypi"): ("apache/airflow", "", 0.92, "Provider package in airflow mono-repo."),
    ("apache-airflow-task-sdk", "pypi"): ("apache/airflow", "", 0.90, "Task SDK in airflow mono-repo."),
    ("apache-beam", "pypi"): ("apache/beam", "", 0.97, "Apache Beam mono-repo."),
    ("argon2-cffi", "pypi"): ("hynek/argon2-cffi", "", 0.95, "Hynek's argon2 wrapper."),
    ("argon2-cffi-bindings", "pypi"): ("hynek/argon2-cffi-bindings", "", 0.95, "Hynek's argon2 cffi bindings."),
    ("azure-identity", "pypi"): ("Azure/azure-sdk-for-python", "", 0.95, "Azure SDK mono-repo."),
    ("azure-keyvault-secrets", "pypi"): ("Azure/azure-sdk-for-python", "", 0.95, "Azure SDK mono-repo."),
    ("azure-mgmt-nspkg", "pypi"): ("Azure/azure-sdk-for-python", "", 0.85, "Namespace package in Azure SDK."),
    ("azure-mgmt-resource", "pypi"): ("Azure/azure-sdk-for-python", "", 0.95, "Azure SDK mono-repo."),
    ("azure-storage-file-datalake", "pypi"): ("Azure/azure-sdk-for-python", "", 0.95, "Azure SDK mono-repo."),
    ("backports-datetime-fromisoformat", "pypi"): ("movermeyer/backports.datetime_fromisoformat", "", 0.85, "Mike Vermeer's backport."),
    ("backports-tarfile", "pypi"): ("jaraco/backports.tarfile", "", 0.85, "Jason R. Coombs's tarfile backport."),
    ("backports-zoneinfo", "pypi"): ("pganssle/zoneinfo", "", 0.85, "Paul Ganssle's stdlib zoneinfo backport."),
    ("bs4", "pypi"): ("", "https://code.launchpad.net/beautifulsoup", 0.65, "Same canonical as beautifulsoup4 (Launchpad/Bazaar)."),
    ("catalogue", "pypi"): ("explosion/catalogue", "", 0.92, "Explosion AI's catalogue library."),
    ("certbot", "pypi"): ("certbot/certbot", "", 0.99, "EFF certbot."),
    ("coloredlogs", "pypi"): ("xolox/python-coloredlogs", "", 0.92, "Peter Odding's coloredlogs."),
    ("contextlib2", "pypi"): ("jazzband/contextlib2", "", 0.85, "jazzband-maintained backport (originally jazzband/contextlib2 or jazzband/community/contextlib2)."),
    ("crashtest", "pypi"): ("sdispater/crashtest", "", 0.90, "Sébastien Eustace's crashtest."),
    ("croniter", "pypi"): ("kiorky/croniter", "", 0.85, "Mathieu Le Marec's cron iterator."),
    ("databricks-sdk", "pypi"): ("databricks/databricks-sdk-py", "", 0.95, "Official Databricks Python SDK."),
    ("discord", "pypi"): ("Rapptz/discord.py", "", 0.50, "Likely an alias to discord.py; could also be unrelated."),
    ("discord-py", "pypi"): ("Rapptz/discord.py", "", 0.95, "Danny Roberts's discord.py."),
    ("docopt", "pypi"): ("docopt/docopt", "", 0.95, "Vladimir Keleshev's docopt."),
    ("docutils", "pypi"): ("", "https://sourceforge.net/p/docutils/code/", 0.70, "Canonical at SourceForge SVN. No widely-known canonical GitHub mirror."),
    ("dulwich", "pypi"): ("jelmer/dulwich", "", 0.95, "Jelmer Vernooij's pure-Python git."),
    ("editables", "pypi"): ("pfmoore/editables", "", 0.92, "Paul Moore's editables (used by hatchling/pip editable installs)."),
    ("enum34", "pypi"): ("", "https://bitbucket.org/stoneleaf/enum34", 0.50, "Older enum backport; canonical historically on Bitbucket."),
    ("events", "pypi"): ("", "", 0.30, "Generic name; couldn't confidently identify the package."),
    ("execnet", "pypi"): ("pytest-dev/execnet", "", 0.92, "Holger Krekel's execnet, maintained under pytest-dev."),
    ("google-auth-httplib2", "pypi"): ("googleapis/google-auth-library-python-httplib2", "", 0.85, "Companion to google-auth, dedicated repo."),
    ("google-cloud-appengine-logging", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "Lives in googleapis/google-cloud-python mono-repo."),
    ("google-cloud-bigquery-storage", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-cloud-dlp", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-cloud-monitoring", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-cloud-resource-manager", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-cloud-secret-manager", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-cloud-vision", "pypi"): ("googleapis/google-cloud-python", "", 0.80, "google-cloud-python mono-repo."),
    ("google-genai", "pypi"): ("googleapis/python-genai", "", 0.80, "Google's GenAI Python SDK."),
    ("gremlinpython", "pypi"): ("apache/tinkerpop", "", 0.92, "Apache TinkerPop mono-repo."),
    ("grpcio-health-checking", "pypi"): ("grpc/grpc", "", 0.92, "grpc/grpc mono-repo."),
    ("gunicorn", "pypi"): ("benoitc/gunicorn", "", 0.97, "Benoît Chesneau's gunicorn."),
    ("hf-xet", "pypi"): ("huggingface/xet-core", "", 0.75, "Hugging Face XET storage core."),
    ("humanfriendly", "pypi"): ("xolox/python-humanfriendly", "", 0.95, "Peter Odding's humanfriendly."),
    ("inspect2", "pypi"): ("", "", 0.30, "Couldn't confidently identify."),
    ("installer", "pypi"): ("pypa/installer", "", 0.95, "PyPA's installer library."),
    ("ipywidgets", "pypi"): ("jupyter-widgets/ipywidgets", "", 0.95, "Jupyter widgets mono-repo."),
    ("jaraco-classes", "pypi"): ("jaraco/jaraco.classes", "", 0.95, "jaraco dot-naming convention."),
    ("jaraco-context", "pypi"): ("jaraco/jaraco.context", "", 0.95, "jaraco dot-naming convention."),
    ("jaraco-functools", "pypi"): ("jaraco/jaraco.functools", "", 0.95, "jaraco dot-naming convention."),
    ("jupyterlab", "pypi"): ("jupyterlab/jupyterlab", "", 0.97, "JupyterLab project."),
    ("jupyterlab-widgets", "pypi"): ("jupyter-widgets/ipywidgets", "", 0.85, "Same ipywidgets mono-repo."),
    ("langchain", "pypi"): ("langchain-ai/langchain", "", 0.97, "LangChain mono-repo."),
    ("langchain-core", "pypi"): ("langchain-ai/langchain", "", 0.95, "LangChain mono-repo."),
    ("lazy-object-proxy", "pypi"): ("ionelmc/python-lazy-object-proxy", "", 0.92, "Ionel Cristian Mărieș's lazy proxy."),
    ("libcst", "pypi"): ("Instagram/LibCST", "", 0.95, "Instagram's concrete syntax tree library."),
    ("librt", "pypi"): ("", "", 0.30, "Couldn't identify."),
    ("lockfile", "pypi"): ("", "", 0.30, "Deprecated package; canonical unclear."),
    ("mako", "pypi"): ("sqlalchemy/mako", "", 0.95, "Mike Bayer's Mako templates, sqlalchemy org."),
    ("ml-dtypes", "pypi"): ("jax-ml/ml_dtypes", "", 0.92, "JAX team's ML data types."),
    ("msal-extensions", "pypi"): ("AzureAD/microsoft-authentication-extensions-for-python", "", 0.90, "Azure AD authentication extensions."),
    ("nbconvert", "pypi"): ("jupyter/nbconvert", "", 0.95, "Jupyter notebook conversion."),
    ("notebook", "pypi"): ("jupyter/notebook", "", 0.95, "Jupyter notebook UI."),
    ("notebook-shim", "pypi"): ("jupyter/notebook_shim", "", 0.92, "Jupyter notebook shim."),
    ("numba", "pypi"): ("numba/numba", "", 0.97, "Numba JIT compiler."),
    ("nvidia-cublas-cu12", "pypi"): ("", "", 0.20, "Proprietary NVIDIA wheel; no public GH."),
    ("nvidia-cuda-nvrtc-cu12", "pypi"): ("", "", 0.20, "Proprietary NVIDIA wheel; no public GH."),
    ("nvidia-cusparse-cu12", "pypi"): ("", "", 0.20, "Proprietary NVIDIA wheel; no public GH."),
    ("nvidia-nccl-cu12", "pypi"): ("NVIDIA/nccl", "", 0.65, "NCCL has a public mirror; cu12 wheel is the binary distribution."),
    ("nvidia-nvjitlink-cu12", "pypi"): ("", "", 0.20, "Proprietary NVIDIA wheel; no public GH."),
    ("oauth2client", "pypi"): ("googleapis/oauth2client", "", 0.85, "Deprecated google-oauth library."),
    ("openpyxl", "pypi"): ("", "https://foss.heptapod.net/openpyxl/openpyxl", 0.70, "Canonical on Heptapod (Mercurial)."),
    ("opentelemetry-instrumentation-requests", "pypi"): ("open-telemetry/opentelemetry-python-contrib", "", 0.92, "OTel Python contrib mono-repo."),
    ("opentelemetry-util-http", "pypi"): ("open-telemetry/opentelemetry-python-contrib", "", 0.92, "OTel Python contrib mono-repo."),
    ("opt-einsum", "pypi"): ("dgasmith/opt_einsum", "", 0.92, "Daniel Smith's opt_einsum."),
    ("ordered-set", "pypi"): ("rspeer/ordered-set", "", 0.92, "Rob Speer's ordered-set."),
    ("pbr", "pypi"): ("openstack/pbr", "", 0.95, "OpenStack's Python Build Reasonableness."),
    ("pendulum", "pypi"): ("sdispater/pendulum", "", 0.95, "Sébastien Eustace's pendulum datetime."),
    ("pg8000", "pypi"): ("tlocke/pg8000", "", 0.92, "Tony Locke's pure-Python Postgres driver."),
    ("pickleshare", "pypi"): ("pickleshare/pickleshare", "", 0.85, "Ville M. Vainio's pickleshare."),
    ("pkginfo", "pypi"): ("", "https://code.launchpad.net/pkginfo", 0.55, "Tres Seaver's pkginfo, canonical historically on Launchpad."),
    ("plotly", "pypi"): ("plotly/plotly.py", "", 0.97, "Plotly Python graphing."),
    ("ply", "pypi"): ("dabeaz/ply", "", 0.95, "David Beazley's PLY parser."),
    ("pycodestyle", "pypi"): ("PyCQA/pycodestyle", "", 0.95, "Python Code Quality Authority."),
    ("pymsalruntime", "pypi"): ("AzureAD/microsoft-authentication-library-for-python", "", 0.55, "Azure AD MSAL Python — runtime extension."),
    ("pymysql", "pypi"): ("PyMySQL/PyMySQL", "", 0.95, "PyMySQL pure-Python MySQL client."),
    ("pyobjc-core", "pypi"): ("ronaldoussoren/pyobjc", "", 0.92, "Ronald Oussoren's PyObjC mono-repo."),
    ("pyreadline3", "pypi"): ("pyreadline3/pyreadline3", "", 0.92, "Active fork of pyreadline."),
    ("pyspark", "pypi"): ("apache/spark", "", 0.97, "Apache Spark mono-repo."),
    ("pytest-cov", "pypi"): ("pytest-dev/pytest-cov", "", 0.97, "pytest coverage plugin under pytest-dev."),
    ("python-json-logger", "pypi"): ("madzak/python-json-logger", "", 0.92, "Zakaria Zajac's python-json-logger."),
    ("pywinpty", "pypi"): ("andfoy/pywinpty", "", 0.92, "Edgar A. Margffoy's pywinpty."),
    ("readme-renderer", "pypi"): ("pypa/readme_renderer", "", 0.95, "PyPA readme renderer."),
    ("requests-file", "pypi"): ("dashea/requests-file", "", 0.85, "David Shea's requests-file transport."),
    ("rfc3986", "pypi"): ("python-hyper/rfc3986", "", 0.92, "URI Reference parser/validator."),
    ("ruamel-yaml", "pypi"): ("", "https://sourceforge.net/p/ruamel-yaml/code/", 0.60, "Anthon van der Neut's YAML library, canonical on SourceForge."),
    ("ruamel-yaml-clibz", "pypi"): ("", "", 0.30, "ruamel sub-library; same canonical as ruamel-yaml."),
    ("scramp", "pypi"): ("tlocke/scramp", "", 0.75, "Tony Locke's SCRAM auth (used by pg8000)."),
    ("selenium", "pypi"): ("SeleniumHQ/selenium", "", 0.97, "Selenium browser automation."),
    ("semver", "pypi"): ("python-semver/python-semver", "", 0.92, "Python semver library."),
    ("structlog", "pypi"): ("hynek/structlog", "", 0.95, "Hynek Schlawack's structured logging."),
    ("tblib", "pypi"): ("ionelmc/python-tblib", "", 0.92, "Ionel Cristian Mărieș's traceback library."),
    ("tensorflow", "pypi"): ("tensorflow/tensorflow", "", 0.99, "Google TensorFlow."),
    ("tensorflow-serving-api", "pypi"): ("tensorflow/serving", "", 0.92, "TensorFlow Serving."),
    ("thrift", "pypi"): ("apache/thrift", "", 0.95, "Apache Thrift mono-repo."),
    ("torch", "pypi"): ("pytorch/pytorch", "", 0.99, "PyTorch."),
    ("typedload", "pypi"): ("ltworf/typedload", "", 0.85, "Salvo \"LtWorf\" Tomaselli's typedload."),
    ("typeguard", "pypi"): ("agronholm/typeguard", "", 0.95, "Alex Grönholm's typeguard."),
    ("types-pyyaml", "pypi"): ("python/typeshed", "", 0.85, "Type stubs in typeshed mono-repo."),
    ("tzlocal", "pypi"): ("regebro/tzlocal", "", 0.92, "Lennart Regebro's local-tz detection."),
    ("uc-micro-py", "pypi"): ("tani/uc.micro-py", "", 0.55, "Port of uc.micro JS to Python; uncertain about exact org."),
    ("unidecode", "pypi"): ("avian2/unidecode", "", 0.85, "Tomaž Šolc's Unidecode."),
    ("uv", "pypi"): ("astral-sh/uv", "", 0.99, "Astral's uv package manager."),
    ("uvloop", "pypi"): ("MagicStack/uvloop", "", 0.97, "MagicStack's uvloop."),
    ("widgetsnbextension", "pypi"): ("jupyter-widgets/ipywidgets", "", 0.85, "Jupyter widgets sub-package."),
    ("xlrd", "pypi"): ("python-excel/xlrd", "", 0.92, "python-excel's xlrd."),
    ("zope-event", "pypi"): ("zopefoundation/zope.event", "", 0.92, "Zope Foundation's event."),
    ("zope-interface", "pypi"): ("zopefoundation/zope.interface", "", 0.97, "Zope Foundation's interface."),

    # ── npm ─────────────────────────────────────────────────────────────────
    ("@types/eslint-scope", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("@types/glob", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("@types/json5", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("@types/minimatch", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("@types/prettier", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("@types/uuid", "npm"): ("DefinitelyTyped/DefinitelyTyped", "", 0.95, "DefinitelyTyped mono-repo."),
    ("atob", "npm"): ("coolaj86/atob", "", 0.70, "AJ ONeal's atob; older deprecated package."),
    ("client-only", "npm"): ("vercel/next.js", "", 0.80, "Next.js internal package; lives in Next mono-repo."),

    # ── crates ──────────────────────────────────────────────────────────────
    ("oorandom", "crates"): ("", "https://hg.sr.ht/~icefox/oorandom", 0.50, "Ico's oorandom; canonical on Mercurial sourcehut."),
    ("ref-cast-test-suite", "crates"): ("dtolnay/ref-cast", "", 0.85, "David Tolnay's ref-cast — test crate in same repo."),
    ("rustc-std-workspace-std", "crates"): ("rust-lang/rust", "", 0.85, "Rust workspace alias crate; lives in rust-lang/rust."),

    # ── cpp: well-known projects ─────────────────────────────────────────────
    ("alsa-lib", "cpp"): ("alsa-project/alsa-lib", "", 0.85, "ALSA project's official mirror."),
    ("apache", "cpp"): ("apache/httpd", "", 0.85, "Apache HTTP Server mirror on apache org."),
    ("apparmor", "cpp"): ("", "https://gitlab.com/apparmor/apparmor.git", 0.85, "AppArmor canonical on gitlab.com."),
    ("apr", "cpp"): ("apache/apr", "", 0.92, "Apache Portable Runtime."),
    ("apr-util", "cpp"): ("apache/apr-util", "", 0.92, "Apache Portable Runtime utils."),
    ("apt", "cpp"): ("Debian/apt", "", 0.75, "Debian apt; mirror on github.com/Debian/apt; canonical at salsa."),
    ("at-spi2-core", "cpp"): ("GNOME/at-spi2-core", "", 0.85, "GNOME at-spi2-core; canonical at gitlab.gnome.org."),
    ("attr", "cpp"): ("", "https://git.savannah.nongnu.org/cgit/attr.git", 0.75, "Attr utility on Savannah; no widely-known GH mirror."),
    ("automake", "cpp"): ("autotools-mirror/automake", "", 0.85, "autotools-mirror org."),
    ("bash", "cpp"): ("bminor/bash", "", 0.92, "Chet Ramey's bash mirror by bminor (same family as bminor/glibc)."),
    ("bind", "cpp"): ("isc-projects/bind9", "", 0.85, "ISC BIND9; mirror on github."),
    ("cpio", "cpp"): ("", "https://git.savannah.gnu.org/cgit/cpio.git", 0.70, "GNU cpio on Savannah."),
    ("dconf", "cpp"): ("GNOME/dconf", "", 0.85, "GNOME dconf mirror."),
    ("diffutils", "cpp"): ("", "https://git.savannah.gnu.org/cgit/diffutils.git", 0.70, "GNU diffutils on Savannah."),
    ("fftw", "cpp"): ("FFTW/fftw3", "", 0.92, "FFTW3 official."),
    ("findutils", "cpp"): ("", "https://git.savannah.gnu.org/cgit/findutils.git", 0.70, "GNU findutils on Savannah."),
    ("freetds", "cpp"): ("FreeTDS/freetds", "", 0.92, "FreeTDS official."),
    ("go", "cpp"): ("golang/go", "", 0.99, "GitHub IS canonical for Go."),
    ("gobject-introspection", "cpp"): ("GNOME/gobject-introspection", "", 0.85, "GNOME gobject-introspection mirror."),
    ("gpgme", "cpp"): ("", "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=gpgme.git", 0.75, "GnuPG project at git.gnupg.org."),
    ("grep", "cpp"): ("", "https://git.savannah.gnu.org/cgit/grep.git", 0.70, "GNU grep on Savannah."),
    ("groff", "cpp"): ("", "https://git.savannah.gnu.org/cgit/groff.git", 0.70, "GNU groff on Savannah."),
    ("grub", "cpp"): ("", "https://git.savannah.gnu.org/cgit/grub.git", 0.70, "GNU GRUB on Savannah."),
    ("gtk", "cpp"): ("GNOME/gtk", "", 0.90, "GNOME GTK mirror."),
    ("guile", "cpp"): ("", "https://git.savannah.gnu.org/cgit/guile.git", 0.70, "GNU Guile on Savannah."),
    ("gzip", "cpp"): ("", "https://git.savannah.gnu.org/cgit/gzip.git", 0.70, "GNU gzip on Savannah."),
    ("iptables", "cpp"): ("", "https://git.netfilter.org/iptables/", 0.75, "Netfilter project."),
    ("json-glib", "cpp"): ("GNOME/json-glib", "", 0.85, "GNOME json-glib mirror."),
    ("kbd", "cpp"): ("legionus/kbd", "", 0.65, "Linux console kbd; uncertain canonical."),
    ("kmod", "cpp"): ("", "https://git.kernel.org/pub/scm/utils/kernel/kmod/kmod.git/", 0.75, "Linux kmod on kernel.org."),
    ("lapack", "cpp"): ("Reference-LAPACK/lapack", "", 0.92, "Reference LAPACK."),
    ("libassuan", "cpp"): ("", "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=libassuan.git", 0.75, "GnuPG libassuan."),
    ("libbluray", "cpp"): ("", "https://git.videolan.org/?p=libbluray.git", 0.75, "VideoLAN libbluray."),
    ("libbsd", "cpp"): ("", "https://gitlab.freedesktop.org/libbsd/libbsd.git", 0.80, "freedesktop libbsd."),
    ("libcap", "cpp"): ("", "https://git.kernel.org/pub/scm/libs/libcap/libcap.git/", 0.80, "Linux libcap on kernel.org."),
    ("libev", "cpp"): ("enki/libev", "", 0.55, "Marc Lehmann's libev; canonical at schmorp.de."),
    ("libgcrypt", "cpp"): ("gpg/libgcrypt", "", 0.75, "GnuPG libgcrypt; gpg/ org."),
    ("libice", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libice.git", 0.85, "X.Org libICE."),
    ("libksba", "cpp"): ("", "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=libksba.git", 0.75, "GnuPG libksba."),
    ("libmd", "cpp"): ("", "https://gitlab.freedesktop.org/libbsd/libmd.git", 0.75, "libmd in libbsd group."),
    ("libmnl", "cpp"): ("", "https://git.netfilter.org/libmnl/", 0.80, "Netfilter libmnl."),
    ("libnetfilter-conntrack", "cpp"): ("", "https://git.netfilter.org/libnetfilter_conntrack/", 0.80, "Netfilter conntrack lib."),
    ("libpciaccess", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libpciaccess.git", 0.85, "X.Org libpciaccess."),
    ("libpipeline", "cpp"): ("", "https://gitlab.com/cjwatson/libpipeline", 0.65, "Colin Watson's libpipeline."),
    ("libpthread-stubs", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libpthread-stubs.git", 0.80, "X.Org libpthread-stubs."),
    ("libsemanage", "cpp"): ("SELinuxProject/selinux", "", 0.85, "SELinux project mono-repo."),
    ("libsepol", "cpp"): ("SELinuxProject/selinux", "", 0.85, "SELinux project mono-repo."),
    ("libsm", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libsm.git", 0.85, "X.Org libSM."),
    ("libthai", "cpp"): ("tlwg/libthai", "", 0.75, "Thai Linux Working Group's libthai."),
    ("libvorbis", "cpp"): ("", "https://gitlab.xiph.org/xiph/vorbis.git", 0.85, "Xiph Vorbis."),
    ("libxaw", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxaw.git", 0.85, "X.Org libXaw."),
    ("libxcursor", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxcursor.git", 0.85, "X.Org libXcursor."),
    ("libxfixes", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxfixes.git", 0.85, "X.Org libXfixes."),
    ("libxi", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxi.git", 0.85, "X.Org libXi."),
    ("libxinerama", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxinerama.git", 0.85, "X.Org libXinerama."),
    ("libxmu", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxmu.git", 0.85, "X.Org libXmu."),
    ("libxpm", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxpm.git", 0.85, "X.Org libXpm."),
    ("libxrandr", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxrandr.git", 0.85, "X.Org libXrandr."),
    ("libxt", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxt.git", 0.85, "X.Org libXt."),
    ("libxtst", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxtst.git", 0.85, "X.Org libXtst."),
    ("libxv", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxv.git", 0.85, "X.Org libXv."),
    ("libxxf86vm", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxxf86vm.git", 0.85, "X.Org libXxf86vm."),
    ("make", "cpp"): ("", "https://git.savannah.gnu.org/cgit/make.git", 0.70, "GNU make on Savannah."),
    ("man-db", "cpp"): ("", "https://gitlab.com/man-db/man-db", 0.75, "Colin Watson's man-db."),
    ("mesa", "cpp"): ("", "https://gitlab.freedesktop.org/mesa/mesa.git", 0.92, "Mesa3D on freedesktop GitLab."),
    ("nano", "cpp"): ("", "https://git.savannah.gnu.org/cgit/nano.git", 0.75, "GNU nano on Savannah."),
    ("netpbm", "cpp"): ("", "https://sourceforge.net/p/netpbm/code/", 0.65, "Netpbm on SourceForge SVN."),
    ("nmap", "cpp"): ("nmap/nmap", "", 0.95, "Nmap official."),
    ("npth", "cpp"): ("", "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=npth.git", 0.75, "GnuPG nPth."),
    ("nss", "cpp"): ("", "https://hg.mozilla.org/projects/nss", 0.65, "Mozilla NSS on hg.mozilla.org (Mercurial)."),
    ("nspr", "cpp"): ("", "https://hg.mozilla.org/projects/nspr", 0.65, "Mozilla NSPR on hg.mozilla.org (Mercurial)."),
    ("opencl-headers", "cpp"): ("KhronosGroup/OpenCL-Headers", "", 0.95, "Khronos OpenCL headers."),
    ("openldap", "cpp"): ("", "https://git.openldap.org/openldap/openldap.git", 0.85, "OpenLDAP project."),
    ("openmpi", "cpp"): ("open-mpi/ompi", "", 0.92, "Open MPI."),
    ("patch", "cpp"): ("", "https://git.savannah.gnu.org/cgit/patch.git", 0.70, "GNU patch on Savannah."),
    ("pciutils", "cpp"): ("", "https://git.kernel.org/pub/scm/utils/pciutils/pciutils.git/", 0.75, "Linux pciutils."),
    ("pinentry", "cpp"): ("", "https://git.gnupg.org/cgi-bin/gitweb.cgi?p=pinentry.git", 0.75, "GnuPG pinentry."),
    ("pulseaudio", "cpp"): ("", "https://gitlab.freedesktop.org/pulseaudio/pulseaudio.git", 0.92, "PulseAudio on freedesktop."),
    ("python:numpy", "cpp"): ("numpy/numpy", "", 0.97, "NumPy (cpp project name has python: prefix from Repology)."),
    ("redis", "cpp"): ("redis/redis", "", 0.97, "Redis."),
    ("rust", "cpp"): ("rust-lang/rust", "", 0.99, "Rust language."),
    ("sed", "cpp"): ("", "https://git.savannah.gnu.org/cgit/sed.git", 0.70, "GNU sed on Savannah."),
    ("sysprof", "cpp"): ("GNOME/sysprof", "", 0.85, "GNOME sysprof mirror."),
    ("tar", "cpp"): ("", "https://git.savannah.gnu.org/cgit/tar.git", 0.70, "GNU tar on Savannah."),
    ("tcl-lang", "cpp"): ("", "https://core.tcl-lang.org/tcl/", 0.65, "Tcl on Fossil VCS."),
    ("tcltk", "cpp"): ("", "https://core.tcl-lang.org/tcl/", 0.55, "Same canonical as tcl-lang (Fossil)."),
    ("texinfo", "cpp"): ("", "https://git.savannah.gnu.org/cgit/texinfo.git", 0.70, "GNU texinfo on Savannah."),
    ("uchardet", "cpp"): ("", "https://gitlab.freedesktop.org/uchardet/uchardet.git", 0.80, "uchardet on freedesktop."),
    ("xauth", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/app/xauth.git", 0.80, "X.Org xauth."),
    ("xft", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/lib/libxft.git", 0.80, "X.Org libXft."),
    ("xorg-server", "cpp"): ("", "https://gitlab.freedesktop.org/xorg/xserver.git", 0.92, "X.Org Server on freedesktop."),
    ("yarn", "cpp"): ("yarnpkg/berry", "", 0.65, "Yarn (could be classic v1 yarnpkg/yarn or modern yarnpkg/berry)."),
    ("python:dbus-python", "cpp"): ("", "https://gitlab.freedesktop.org/dbus/dbus-python.git", 0.80, "dbus-python on freedesktop."),
}


def main() -> None:
    # Read existing rows
    rows = list(csv.DictReader(open(TARGET, encoding="utf-8")))
    by_key: dict[tuple[str, str], dict] = {(r["package"], r["ecosystem"]): r for r in rows}

    added = 0
    updated = 0
    for (pkg, eco), (gh, canon, conf, reason) in EXTRAS.items():
        host = ""
        if not canon and gh:
            canon = f"https://github.com/{gh}.git"
        if canon:
            try:
                from urllib.parse import urlparse
                host = (urlparse(canon).hostname or "").lower()
            except Exception:
                pass
        gh_mirror = bool(host) and host != "github.com"

        new_row = {
            "package": pkg, "ecosystem": eco, "value_class": "C",
            "github_repo": gh, "canonical_git_url": canon,
            "gh_mirror": str(gh_mirror), "confidence": f"{conf:.2f}",
            "self_confidence_raw": f"{conf:.2f}",
            "verified": "true" if gh else "",
            "agreement": "claude-knowledge+http", "n_runs": "1", "dissent": "",
            "tier": "claude-guess", "source": "training data + HTTP HEAD",
            "reasoning": reason, "review_note": "",
            "checked_at": NOW, "model": MODEL,
        }
        if (pkg, eco) in by_key:
            by_key[(pkg, eco)] = new_row
            updated += 1
        else:
            by_key[(pkg, eco)] = new_row
            added += 1

    out = sorted(by_key.values(), key=lambda r: (r["ecosystem"], r["package"]))
    with open(TARGET, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(out)

    print(f"Added: {added}, updated: {updated}, total rows: {len(out)}")


if __name__ == "__main__":
    main()
