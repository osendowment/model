"""Regression tests for build_complexity's lizard false-zero guard.

Background: lizard is keyed on the same commits-years SHA as scc, but a
shared CI/template commit (fork-network leak) made lizard report 0 functions
for repos whose real code scc measured fine (setuptools 82K sloc → 0 funcs,
clap 60K → 0). The guard treats such impossible scc>0/lizard==0 pairs as
missing so a false cyclomatic_max=0 can't deflate the complexity score.
"""

from src.risk.build_complexity import (
    LIZARD_FALSE_ZERO_MIN_SCC_CX,
    _is_lizard_false_zero,
)


def _scc(cx):
    return {"loc": "50000", "sloc": "40000", "complexity": str(cx)}


def _lz(cyclo_total, cyclo_max="0"):
    return {"cyclomatic_total": str(cyclo_total), "cyclomatic_max": cyclo_max}


def test_setuptools_style_false_zero_is_flagged():
    # scc found 7477 branches but lizard found 0 functions → off-mainline tree.
    assert _is_lizard_false_zero(_scc(7477), _lz(0)) is True


def test_small_real_repo_false_zero_is_flagged():
    # pytest-perf: scc complexity 20, lizard 0 functions → still a failure.
    assert _is_lizard_false_zero(_scc(20), _lz(0)) is True


def test_genuine_data_module_zero_is_kept():
    # color-name / tagged-tag style: ~no branching AND 0 functions is honest.
    assert _is_lizard_false_zero(_scc(1), _lz(0)) is False
    assert _is_lizard_false_zero(_scc(0), _lz(0)) is False


def test_real_lizard_data_is_never_flagged():
    # A repo with measured functions is never a false zero.
    assert _is_lizard_false_zero(_scc(7477), _lz(16541, cyclo_max="77")) is False


def test_absent_lizard_is_not_a_false_zero():
    # Missing lizard rows are already "missing", not a zero to scrub.
    assert _is_lizard_false_zero(_scc(7477), {}) is False


def test_threshold_boundary():
    # Exactly at the threshold flags; just below it is spared.
    assert _is_lizard_false_zero(_scc(LIZARD_FALSE_ZERO_MIN_SCC_CX), _lz(0)) is True
    assert _is_lizard_false_zero(_scc(LIZARD_FALSE_ZERO_MIN_SCC_CX - 1), _lz(0)) is False
