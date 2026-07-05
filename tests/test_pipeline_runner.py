"""Tests for the shared pipeline orchestration helper."""

import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.common.pipeline_runner import Step, run_pipeline, select_steps

STEPS = [
    Step("a", "m.a", fetch=True),
    Step("b", "m.b"),
    Step("c", "m.c", fetch=True),
    Step("d", "m.d"),
    Step("e", "m.e", fetch=True, pipeline=True),
    Step("f", "m.f", net=True),
]


def test_select_all():
    assert [s.label for s in select_steps(STEPS, None, None)] == [
        "a", "b", "c", "d", "e", "f"
    ]


def test_select_only():
    assert [s.label for s in select_steps(STEPS, None, "c")] == ["c"]


def test_select_from():
    assert [s.label for s in select_steps(STEPS, "b", None)] == [
        "b", "c", "d", "e", "f"
    ]


def test_all_steps_always_run():
    # No skip-fetch: all steps (including fetch=True ones) are always returned.
    result = select_steps(STEPS, None, None)
    assert len(result) == len(STEPS)


def test_select_from_includes_fetch_steps():
    # Even fetch=True steps are included when using --from.
    result = select_steps(STEPS, "a", None)
    assert [s.label for s in result] == ["a", "b", "c", "d", "e", "f"]


def test_unknown_step_raises():
    with pytest.raises(KeyError):
        select_steps(STEPS, None, "zzz")
    with pytest.raises(KeyError):
        select_steps(STEPS, "zzz", None)


def _make_args(offline: bool = False, refresh: bool = False,
               from_step=None, only=None, list_=False) -> argparse.Namespace:
    return argparse.Namespace(
        offline=offline,
        refresh=refresh,
        from_step=from_step,
        only=only,
        list=list_,
    )


def _run_and_capture(steps, args):
    """Run pipeline with subprocess.run mocked; return list of called argvs."""
    called = []

    def fake_run(cmd, **kwargs):
        called.append(list(cmd))
        r = MagicMock()
        r.returncode = 0
        return r

    with patch("src.common.pipeline_runner.subprocess.run", side_effect=fake_run):
        rc = run_pipeline(steps, args)
    return rc, called


def test_run_pipeline_no_flags():
    """Net/pipeline steps get no extra flags when neither --offline nor --refresh is set."""
    steps = [
        Step("a", "m.a"),
        Step("b", "m.b", net=True),
        Step("c", "m.c", pipeline=True),
    ]
    args = _make_args()
    rc, called = _run_and_capture(steps, args)
    assert rc == 0
    assert called == [
        [sys.executable, "-m", "m.a"],
        [sys.executable, "-m", "m.b"],
        [sys.executable, "-m", "m.c"],
    ]


def test_run_pipeline_offline_forwarded_to_net_and_pipeline():
    """--offline is appended only to net=True and pipeline=True steps."""
    steps = [
        Step("plain", "m.plain"),
        Step("netty", "m.netty", net=True),
        Step("orch", "m.orch", pipeline=True),
    ]
    args = _make_args(offline=True)
    rc, called = _run_and_capture(steps, args)
    assert rc == 0
    assert called[0] == [sys.executable, "-m", "m.plain"]          # no flag
    assert called[1] == [sys.executable, "-m", "m.netty", "--offline"]
    assert called[2] == [sys.executable, "-m", "m.orch", "--offline"]


def test_run_pipeline_refresh_forwarded_to_net_and_pipeline():
    """--refresh is appended only to net=True and pipeline=True steps."""
    steps = [
        Step("plain", "m.plain"),
        Step("netty", "m.netty", net=True),
        Step("orch", "m.orch", pipeline=True),
    ]
    args = _make_args(refresh=True)
    rc, called = _run_and_capture(steps, args)
    assert rc == 0
    assert called[0] == [sys.executable, "-m", "m.plain"]
    assert called[1] == [sys.executable, "-m", "m.netty", "--refresh"]
    assert called[2] == [sys.executable, "-m", "m.orch", "--refresh"]


def test_run_pipeline_both_flags():
    """Both flags are forwarded together."""
    steps = [Step("netty", "m.netty", net=True)]
    args = _make_args(offline=True, refresh=True)
    rc, called = _run_and_capture(steps, args)
    assert rc == 0
    assert "--offline" in called[0]
    assert "--refresh" in called[0]


def test_run_pipeline_returns_nonzero_on_failure():
    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 1
        return r

    steps = [Step("a", "m.a")]
    args = _make_args()
    with patch("src.common.pipeline_runner.subprocess.run", side_effect=fake_run):
        rc = run_pipeline(steps, args)
    assert rc == 1
