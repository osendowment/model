"""Regression tests for the Sonar Cognitive Complexity implementation.

Two parallel paths to validate:
    1. `cognitive_complexity` PyPI lib  → Python files (AST-based, accurate).
    2. `SonarCognitiveExt` Lizard hook  → C/C++/Java/JS/TS/Rust/Go/etc. files.

Reference values are taken from the Sonar paper
(https://www.sonarsource.com/docs/CognitiveComplexity.pdf) Figure 1 and
chapter 3 examples. We compare both paths against those expected numbers.
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest

from src.sources.git.fetch_sha_metrics import (
    SonarCognitiveExt,
    _python_cognitive,
    _run_lizard_cognitive,
)


# ───────────────────────────── helpers ─────────────────────────────────────

def _measure_lizard(src: str, suffix: str) -> dict[str, int]:
    """Run our extension on `src`. Returns {func_name: cognitive}."""
    import lizard
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(src)
        path = f.name
    try:
        ext = SonarCognitiveExt()
        exts = lizard.get_extensions([ext])
        out: dict[str, int] = {}
        for r in lizard.analyze_files([path], exts=exts):
            for fn in r.function_list:
                out[fn.name] = getattr(fn, "cognitive_complexity", -1)
        return out
    finally:
        Path(path).unlink(missing_ok=True)


def _measure_python(src: str) -> dict[str, int]:
    """Run cognitive_complexity AST lib on `src`. Returns {func_name: cognitive}."""
    from cognitive_complexity.api import get_cognitive_complexity
    tree = ast.parse(src)
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = get_cognitive_complexity(node)
    return out


# ───────────────────────── Sonar paper examples ────────────────────────────

# Reference: https://www.sonarsource.com/docs/CognitiveComplexity.pdf §3.2
# Java/C-style code, exactly as in the paper.
SONAR_C = """
int sumOfPrimes(int max) {
    int total = 0;
    OUT: for (int i = 1; i <= max; ++i) {
        for (int j = 2; j < i; ++j) {
            if (i % j == 0) {
                goto OUT;
            }
        }
        total += i;
    }
    return total;
}

String getWords(int number) {
    if (number == 1) {
        return "one";
    } else if (number == 2) {
        return "a couple";
    } else if (number == 3) {
        return "a few";
    } else {
        return "lots";
    }
}
"""


def test_sonar_paper_sum_of_primes_c():
    """Paper: sumOfPrimes = 7 (for=1, nested for=2, nested if=3, goto=1)."""
    out = _measure_lizard(SONAR_C, ".c")
    assert out["sumOfPrimes"] == 7


def test_sonar_paper_get_words_c():
    """Paper: getWords = 4 (if=1, else if=1, else if=1, else=1)."""
    out = _measure_lizard(SONAR_C, ".c")
    assert out["getWords"] == 4


def test_sonar_logical_operator_chain_c():
    """`a && b || c && !d`: +1 (&&), +1 (||→toggle), +1 (&&→toggle) = 3."""
    src = "int check(int a, int b, int c, int d) { return a && b || c && !d; }\n"
    out = _measure_lizard(src, ".c")
    assert out["check"] == 3


def test_lizard_trivial_zero_c():
    """No control flow → 0."""
    src = "void empty(void) { return; }\n"
    out = _measure_lizard(src, ".c")
    assert out["empty"] == 0


def test_lizard_javascript_nested_blocks():
    """JS: for=1 + nested if=2 + nested if=3 + && in cond=1 + else=1 = 8."""
    src = """
function process(items) {
    for (const item of items) {
        if (item.value > 0) {
            if (item.type === 'A' && item.priority > 5) {
                console.log(item);
            }
        } else {
            return null;
        }
    }
    return items.length;
}
"""
    out = _measure_lizard(src, ".js")
    assert out["process"] == 8


def test_lizard_rust_nested_if_else():
    """Rust: for=1 + nested if=2 + nested if=3 + else=1 = 7. Tolerate +/-1
    because Lizard's Rust tokenizer occasionally splits compound tokens."""
    src = """
fn check(items: &[i32]) -> i32 {
    let mut total = 0;
    for x in items {
        if *x > 0 {
            if x % 2 == 0 {
                total += x;
            } else {
                total -= x;
            }
        }
    }
    total
}
"""
    out = _measure_lizard(src, ".rs")
    assert 6 <= out["check"] <= 8


# ────────────────────────────── Python lib ─────────────────────────────────

def test_python_get_words_4():
    src = '''
def get_words(number):
    if number == 1: return "one"
    elif number == 2: return "a couple"
    elif number == 3: return "a few"
    else: return "lots"
'''
    out = _measure_python(src)
    assert out["get_words"] == 4


def test_python_sum_of_primes_6():
    """Python equivalent without goto → Sonar reference 6 (for+nested for+nested if = 6)."""
    src = '''
def sum_of_primes(maxn):
    total = 0
    for i in range(1, maxn + 1):
        for j in range(2, i):
            if i % j == 0:
                break
        total += i
    return total
'''
    out = _measure_python(src)
    assert out["sum_of_primes"] == 6


def test_python_trivial_zero():
    src = "def trivial(x):\n    return x + 1\n"
    out = _measure_python(src)
    assert out["trivial"] == 0


def test_python_async_function():
    """`async def` and `async for` are handled — try/except inside add nesting."""
    src = '''
async def fetcher(items):
    results = []
    async for item in items:
        if item.valid:
            try:
                data = await item.fetch()
                if data and data.score > 0.5:
                    results.append(data)
            except (TimeoutError, ConnectionError):
                continue
    return results
'''
    out = _measure_python(src)
    assert out["fetcher"] >= 5  # for + if + try + nested if + (logical) + except


# ─────────────────────────── _python_cognitive() ───────────────────────────

def test_python_cognitive_file_aggregation(tmp_path):
    """Aggregator returns (n_funcs, total, max) over a file."""
    src = '''
def a():
    if True:
        return 1
def b():
    for i in range(10):
        if i % 2 == 0:
            for j in range(i):
                pass
def c(): return 1
'''
    p = tmp_path / "x.py"
    p.write_text(src)
    n, total, mx = _python_cognitive(str(p))
    assert n == 3
    assert total == 1 + 6 + 0  # a=1, b=6 (for+if+nested for=1+2+3), c=0
    assert mx == 6


def test_python_cognitive_handles_syntax_error(tmp_path):
    """Garbage source returns zeros, doesn't raise."""
    p = tmp_path / "bad.py"
    p.write_text("def broken( :\n  pass\n")
    n, total, mx = _python_cognitive(str(p))
    assert (n, total, mx) == (0, 0, 0)


# ─────────────────────────── _run_lizard_cognitive() ───────────────────────

def test_lizard_aggregator_files_arg(tmp_path):
    """Aggregator returns (n_funcs, total, max) over a list of paths."""
    a = tmp_path / "a.c"
    a.write_text("int f(void) { if (1) { return 0; } return 1; }\n")
    b = tmp_path / "b.c"
    b.write_text("int g(void) { for (int i=0;i<10;++i) { if (i&1) continue; } return 0; }\n")
    n, total, mx = _run_lizard_cognitive([str(a), str(b)])
    # f: if=+1 → 1; g: for=+1, nested if=+2 → 3
    assert n == 2
    assert total == 4
    assert mx == 3


def test_lizard_aggregator_empty():
    assert _run_lizard_cognitive([]) == (0, 0, 0)


# ─────────────────────── build_complexity wiring tests ─────────────────────

def test_build_complexity_reads_cognitive_from_lizard(tmp_path, monkeypatch):
    """build_complexity surfaces cognitive_* from the sha-pinned lizard.csv.

    Cognitive metrics were consolidated out of a standalone cognitive.csv
    into the long-format data/sources/git/lizard.csv; build_complexity picks them
    up at the same snapshot sha it uses for scc.
    """
    from src.common.repos import RepoEntry
    from src.risk import build_complexity

    sha = "a" * 40
    (tmp_path / "commits-years.csv").write_text(
        "repo,repo_id,year,commits,last_sha\n"
        f"foo/bar,1,2025,120,{sha}\n"
    )
    (tmp_path / "scc.csv").write_text(
        "repo,repo_id,commit_sha,metric,value,checked_at\n"
        f"foo/bar,1,{sha},loc,5000,2025-01-01T00:00:00Z\n"
    )
    (tmp_path / "lizard.csv").write_text(
        "repo,repo_id,commit_sha,metric,value,checked_at\n"
        f"foo/bar,1,{sha},cognitive_total,42,2025-01-01T00:00:00Z\n"
        f"foo/bar,1,{sha},cognitive_avg,4.2,2025-01-01T00:00:00Z\n"
        f"foo/bar,1,{sha},cognitive_max,15,2025-01-01T00:00:00Z\n"
    )
    monkeypatch.setattr(build_complexity, "COMMITS_YEARS_FILE",
                        tmp_path / "commits-years.csv")
    monkeypatch.setattr(build_complexity, "SCC_FILE", tmp_path / "scc.csv")
    monkeypatch.setattr(build_complexity, "LIZARD_FILE", tmp_path / "lizard.csv")
    monkeypatch.setattr(build_complexity, "load_top_repos",
                        lambda: [RepoEntry(repo="foo/bar", repo_id="1")])

    rows = build_complexity.build()
    assert len(rows) == 1
    row = rows[0]
    assert row["cognitive_total"] == "42"
    assert row["cognitive_avg"] == "4.2"
    assert row["cognitive_max"] == "15"
    assert row["loc_eoy"] == "5000"


def test_build_complexity_includes_cognitive_columns():
    """FIELDS list (the CSV header order) includes the three new columns."""
    from src.risk.build_complexity import FIELDS
    for col in ("cognitive_total", "cognitive_avg", "cognitive_max"):
        assert col in FIELDS, f"{col} missing from build_complexity FIELDS"


def test_cognitive_pass_skips_fortran(tmp_path, monkeypatch):
    """The cognitive pass must apply LIZARD_SKIP_SUFFIXES like the cyclomatic
    pass does — lizard's fixed-form Fortran reader OOM-kills on large sources
    (scipy's d_odr.f took the whole analysis subprocess down with exit -9;
    the filter originally existed only in _run_lizard)."""
    from src.sources.git import fetch_sha_metrics as fsm

    fortran = tmp_path / "d_odr.f"
    fortran.write_text("      SUBROUTINE DODR\n      END\n")
    c_file = tmp_path / "ok.c"
    c_file.write_text("int f(int x) { if (x) { return 1; } return 0; }\n")

    seen: list[list[str]] = []
    real_analyze = __import__("lizard").analyze_files

    def spy(files, exts=None):
        seen.append(list(files))
        return real_analyze(files, exts=exts)

    monkeypatch.setattr("lizard.analyze_files", spy)
    n, total, mx = fsm._run_lizard_cognitive([str(fortran), str(c_file)])
    assert all(str(fortran) not in batch for batch in seen), \
        "Fortran file reached lizard.analyze_files in the cognitive pass"
    assert n >= 1  # the C file was still analyzed


def test_cognitive_pass_all_fortran_returns_zero():
    """An all-Fortran input must return zeros, not crash on an empty list."""
    from src.sources.git import fetch_sha_metrics as fsm
    assert fsm._run_lizard_cognitive(["only.f", "more.f90"]) == (0, 0, 0)
