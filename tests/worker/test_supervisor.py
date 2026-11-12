"""Supervisor: deadline checking, 300 s heartbeat, SIGTERM/SIGKILL escalation."""

from __future__ import annotations

import pytest

from vg_worker.supervisor import DeadlineExceeded, Supervisor

from conftest import Clock, FakeSignaler, SleepRecorder


def test_remaining_decreases_and_deadline_raises() -> None:
    clock = Clock(0.0)
    supervisor = Supervisor(3600.0, clock=clock)
    with supervisor:
        assert supervisor.remaining() == pytest.approx(3600.0)
        clock.advance(3599.0)
        assert supervisor.remaining() == pytest.approx(1.0)
        supervisor.check()
        clock.advance(2.0)
        with pytest.raises(DeadlineExceeded):
            supervisor.check()
        assert supervisor.deadline_exceeded() is True


def test_supervisor_needs_enter() -> None:
    supervisor = Supervisor(10.0, clock=Clock(0.0))
    with pytest.raises(RuntimeError):
        supervisor.remaining()


def test_supervisor_not_reenterable() -> None:
    clock = Clock(0.0)
    supervisor = Supervisor(10.0, clock=clock)
    with supervisor:
        with pytest.raises(RuntimeError):
            with supervisor:
                pass


def test_heartbeat_fires_every_300_seconds() -> None:
    clock = Clock(0.0)
    beats: list[int] = []
    supervisor = Supervisor(3600.0, heartbeat_sec=300.0, heartbeat=lambda: beats.append(1), clock=clock)
    with supervisor:
        clock.advance(299.0)
        assert supervisor.heartbeat_if_due() is False
        assert beats == []
        clock.advance(1.0)  # 300 s elapsed
        assert supervisor.heartbeat_if_due() is True
        assert beats == [1]
        clock.advance(0.5)
        assert supervisor.heartbeat_if_due() is False
        clock.advance(300.0)
        assert supervisor.heartbeat_if_due() is True
        assert beats == [1, 1]


def test_tick_raises_deadline_and_not_heartbeat_first_beat() -> None:
    clock = Clock(0.0)
    beats: list[int] = []
    supervisor = Supervisor(
        3600.0,
        heartbeat_sec=300.0,
        heartbeat=lambda: beats.append(1),
        clock=clock,
        sleeper=SleepRecorder(),
        signaler=FakeSignaler(),
    )
    with supervisor:
        clock.advance(3601.0)
        with pytest.raises(DeadlineExceeded):
            supervisor.tick()
        # the deadline fired; enforcement happens in main via enforce_deadline
        assert supervisor.deadline_triggered is False


def test_enforce_deadline_escalates_term_then_kill() -> None:
    clock = Clock(0.0)
    signaler = FakeSignaler()
    sleep = SleepRecorder()
    supervisor = Supervisor(3600.0, clock=clock, sleeper=sleep, signaler=signaler, kill_grace_sec=45.0)
    supervisor.enforce_deadline()
    assert signaler.signals == ["TERM", "KILL"]
    assert list(sleep.sleeps) == [45.0]
    assert supervisor.deadline_triggered is True
    supervisor.enforce_deadline()  # idempotent: no second escalation
    assert signaler.signals == ["TERM", "KILL"]
    assert list(sleep.sleeps) == [45.0]


def test_enforce_deadline_without_signaler_is_safe() -> None:
    clock = Clock(0.0)
    supervisor = Supervisor(3600.0, clock=clock, sleeper=SleepRecorder(), signaler=None)
    supervisor.enforce_deadline()
    assert supervisor.deadline_triggered is True


def test_background_heartbeat_fires_during_a_stage() -> None:
    import time

    beats: list[int] = []
    supervisor = Supervisor(
        deadline_sec=60,
        heartbeat_sec=0.01,
        heartbeat=lambda: beats.append(1),
    )
    with supervisor:
        supervisor.start_background(poll_sec=0.005)
        deadline = time.monotonic() + 2.0
        while len(beats) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
    assert len(beats) >= 2


def test_child_registry_signals_only_registered_groups(monkeypatch) -> None:
    import os

    from vg_worker.supervisor import ChildRegistry

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    registry = ChildRegistry()
    registry.register(101)
    registry.register(202)
    assert registry.signal(15) == 2
    assert calls == [(101, 15), (202, 15)]


def test_background_lock_loss_aborts_the_next_tick() -> None:
    import time

    from vg_pipeline.errors import PipelineError

    def heartbeat() -> None:
        raise PipelineError("LOCK_CONFLICT", "lost the lock")

    supervisor = Supervisor(
        deadline_sec=60,
        heartbeat_sec=0.01,
        heartbeat=heartbeat,
        kill_grace_sec=0,
    )
    with supervisor:
        supervisor.start_background(poll_sec=0.005)
        deadline = time.monotonic() + 2.0
        while supervisor.background_error is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert supervisor.background_error is not None
        with pytest.raises(PipelineError):
            supervisor.tick()
