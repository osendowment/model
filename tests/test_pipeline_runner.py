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


def _make_args(refresh: bool = False,
               from_step=None, only=None, list_=False) -> argparse.Namespace:
    return argparse.Namespace(
        refresh=refresh,
        from_step=from_step,
        only=only,
        list=list_,
    )


class _FakeProc:
    """Popen stand-in: instant success with empty output."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        import io
        self.stdout = io.StringIO("")

    def poll(self):
        return self.returncode


def _run_and_capture(steps, args, returncode=0):
    """Run pipeline with subprocess.Popen mocked; return list of called argvs."""
    called = []

    def fake_popen(cmd, **kwargs):
        called.append(list(cmd))
        return _FakeProc(returncode)

    with patch("src.common.pipeline_runner.subprocess.Popen", side_effect=fake_popen):
        rc = run_pipeline(steps, args)
    return rc, called


def test_run_pipeline_no_flags():
    """Net/pipeline steps get no extra flags when --refresh is not set."""
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


def test_run_pipeline_returns_nonzero_on_failure():
    steps = [Step("a", "m.a")]
    args = _make_args()
    rc, _ = _run_and_capture(steps, args, returncode=1)
    assert rc == 1


def test_select_steps_never_drops_a_step():
    """EVERY selected step runs. There is no mode that silently omits fetchers.

    The old --offline dropped every `fetch=True and not net=True` step. Those
    steps did not fall back to their caches — they simply did not execute, so
    their outputs went stale or empty while the run still reported success.
    That is how a value rebuild plus an offline eligibility run reduced 900
    registry licences to blanks with all 83 health checks green. Cache policy
    is now the per-fetcher TTL in settings.json, which no-ops a warm fetcher
    instead of removing it from the run.
    """
    from src.common.pipeline_runner import Step, select_steps
    steps = [
        Step("fetcher", "m.fetch", fetch=True),
        Step("netstep", "m.net", net=True),
        Step("ecopipe", "m.eco", fetch=True, pipeline=True),
        Step("builder", "m.build"),
    ]
    assert [s.label for s in select_steps(steps, None, None)] == [
        "fetcher", "netstep", "ecopipe", "builder"]
