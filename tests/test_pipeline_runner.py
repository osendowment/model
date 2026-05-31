"""Tests for the shared pipeline orchestration helper."""

import pytest

from src.common.pipeline_runner import Step, select_steps

STEPS = [
    Step("a", "m.a", fetch=True),
    Step("b", "m.b"),
    Step("c", "m.c", fetch=True),
    Step("d", "m.d"),
    Step("e", "m.e", fetch=True, pipeline=True),
]


def test_select_all():
    assert [s.label for s in select_steps(STEPS, None, None, False)] == ["a", "b", "c", "d", "e"]


def test_select_only():
    assert [s.label for s in select_steps(STEPS, None, "c", False)] == ["c"]


def test_select_from():
    assert [s.label for s in select_steps(STEPS, "b", None, False)] == ["b", "c", "d", "e"]


def test_select_skip_fetch_drops_fetch_keeps_pipeline():
    # plain fetch steps (a, c) dropped; pipeline step (e) survives
    assert [s.label for s in select_steps(STEPS, None, None, True)] == ["b", "d", "e"]


def test_select_from_plus_skip_fetch():
    assert [s.label for s in select_steps(STEPS, "b", None, True)] == ["b", "d", "e"]


def test_unknown_step_raises():
    with pytest.raises(KeyError):
        select_steps(STEPS, None, "zzz", False)
    with pytest.raises(KeyError):
        select_steps(STEPS, "zzz", None, False)
