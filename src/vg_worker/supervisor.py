"""Job deadline and heartbeat handling for one SQS message.

The worker enforces the job deadline ITSELF (3600 s, never the old FFmpeg
100 s timeout bug). :class:`Supervisor` is deliberately simple and testable
WITHOUT real threads: it runs inline, driven by :meth:`tick` calls from the
pipeline loop, with an injectable clock and sleep.

* :meth:`check` raises :class:`DeadlineExceeded` once the deadline passes;
* every ``heartbeat_sec`` (300 s) the injected heartbeat callback runs —
  production uses it for ``sqs:ChangeMessageVisibility`` and the S3 lock
  renewal (CONTRACT.md section 2);
* at the deadline :meth:`enforce_deadline` sends SIGTERM and, after the
  30-60 s grace window, SIGKILL (via an injected :class:`Signaler`).
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
from typing import Protocol

from vg_pipeline.logging import get_logger

log = get_logger("vg_worker.supervisor")

AnyClock = Callable[[], float]
AnySleep = Callable[[float], None]
HeartbeatFn = Callable[[], None]


class DeadlineExceeded(Exception):
    """The 3600 s job deadline passed.

    NOT a :class:`vg_pipeline.errors.PipelineError` on purpose: it is an
    infrastructure cutoff, not a media-pipeline verdict, so the SQS message
    is left visible (transient) and the worker terminates.
    """


class Signaler(Protocol):
    """Send signals to the worker's child processes (docker CLI, tools)."""

    def sigterm(self) -> None: ...

    def sigkill(self) -> None: ...


class ChildRegistry:
    """PIDs of children launched by the worker (thread-safe).

    Every child is started in its own session/process group
    (``start_new_session=True``), so signalling a registered PID's group reaches
    that child and its descendants but NEVER the supervisor itself. The old
    ``os.killpg(os.getpgrp(), ...)`` included the supervisor and killed it
    before the grace period, failure reporting, or instance-retirement
    ``finally`` block could run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pids: set[int] = set()

    def register(self, pid: int) -> None:
        with self._lock:
            self._pids.add(pid)

    def unregister(self, pid: int) -> None:
        with self._lock:
            self._pids.discard(pid)

    def snapshot(self) -> list[int]:
        with self._lock:
            return sorted(self._pids)

    def signal(self, signum: int) -> int:
        """Signal every registered child's process group; returns the count."""
        sent = 0
        for pid in self.snapshot():
            try:
                os.killpg(pid, signum)
                sent += 1
            except ProcessLookupError:
                self.unregister(pid)
            except PermissionError:
                # A child that changed group/uid cannot be signalled; the
                # unit's KillMode=control-group is the final fallback.
                log.warning("cannot signal child process group", extra={"pid": pid})
        return sent


CHILDREN = ChildRegistry()


class ProcessGroupSignaler:
    """Signal the worker's CHILD process groups, never the supervisor."""

    def __init__(self, children: ChildRegistry | None = None) -> None:
        self._children = children or CHILDREN

    def sigterm(self) -> None:
        sent = self._children.signal(signal.SIGTERM)
        log.info("SIGTERM sent to child process groups", extra={"children": sent})

    def sigkill(self) -> None:
        sent = self._children.signal(signal.SIGKILL)
        log.info("SIGKILL sent to child process groups", extra={"children": sent})


class Supervisor:
    """One job's deadline, heartbeat and child-kill escalation."""

    def __init__(
        self,
        deadline_sec: float = 3600.0,
        *,
        heartbeat_sec: float = 300.0,
        heartbeat: "HeartbeatFn | None" = None,
        clock: "AnyClock | None" = None,
        sleeper: "AnySleep | None" = None,
        signaler: "Signaler | None" = None,
        kill_grace_sec: float = 45.0,
    ) -> None:
        self._deadline_sec = float(deadline_sec)
        self._heartbeat_sec = float(heartbeat_sec)
        self._heartbeat_fn = heartbeat
        self._clock: AnyClock = clock or time.monotonic
        self._sleeper: AnySleep = sleeper or time.sleep
        self._signaler = signaler
        self._kill_grace_sec = float(kill_grace_sec)
        self._started: float | None = None
        self._last_heartbeat: float | None = None
        self._deadline_triggered = False
        self._heartbeat_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._background_error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Supervisor":
        if self._started is not None:
            raise RuntimeError("Supervisor is not re-enterable")
        self._started = self._clock()
        self._last_heartbeat = self._started
        log.info("supervisor started", extra={"deadline_sec": self._deadline_sec, "heartbeat_sec": self._heartbeat_sec})
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop_background()
        return None

    # -- state -------------------------------------------------------------

    def remaining(self) -> float:
        """Seconds left before the deadline (``< 0`` once it passed)."""
        if self._started is None:
            raise RuntimeError("Supervisor must be entered (used as a context manager) first")
        return self._deadline_sec - (self._clock() - self._started)

    def deadline_exceeded(self) -> bool:
        """True when :meth:`check` would raise right now."""
        return self.remaining() <= 0.0

    @property
    def deadline_triggered(self) -> bool:
        """True once the deadline fired and :meth:`enforce_deadline` ran."""
        return self._deadline_triggered

    # -- actions -----------------------------------------------------------

    def heartbeat_if_due(self) -> bool:
        """Run the injected heartbeat when ``heartbeat_sec`` elapsed.

        The FIRST heartbeat is due one full interval after start, matching
        the "every 300 s while a job runs" wording.
        """
        if self._started is None:
            raise RuntimeError("Supervisor must be entered first")
        assert self._last_heartbeat is not None
        with self._heartbeat_lock:
            elapsed = self._clock() - self._last_heartbeat
            if elapsed < self._heartbeat_sec:
                return False
            self._last_heartbeat = self._clock()
            if self._heartbeat_fn is not None:
                self._heartbeat_fn()
        log.debug("heartbeat fired", extra={"interval_sec": self._heartbeat_sec})
        return True

    def check(self) -> None:
        """Raise :class:`DeadlineExceeded` when the deadline passed."""
        if self.deadline_exceeded():
            raise DeadlineExceeded(f"job deadline of {self._deadline_sec:.0f}s exceeded")

    def tick(self) -> None:
        """One pipeline-loop turn: surface a background failure, beat, check."""
        self.raise_background_error()
        self.heartbeat_if_due()
        self.check()

    @property
    def background_error(self) -> BaseException | None:
        """The first fatal error raised by the background heartbeat, if any."""
        return self._background_error

    def raise_background_error(self) -> None:
        """Re-raise a background failure at the next stage boundary.

        A lost lock / SQS outage raised by the heartbeat MUST abort this
        attempt (CONTRACT.md section 2); swallowing it would let a long stage
        keep burning GPU after the lock was taken by another worker.
        """
        error = self._background_error
        if error is not None:
            raise error

    # -- background heartbeat ---------------------------------------------

    def start_background(self, poll_sec: float = 1.0) -> None:
        """Run :meth:`tick` on a daemon thread while a stage is executing.

        Ticks previously happened only BETWEEN synchronous stages, so a single
        generation/encode/package/upload longer than the SQS visibility timeout
        allowed redelivery, and a hung subprocess could run indefinitely.
        """
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._background_loop,
            args=(poll_sec,),
            name="vg-supervisor",
            daemon=True,
        )
        self._thread.start()

    def _background_loop(self, poll_sec: float) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except DeadlineExceeded:
                self.enforce_deadline()
                return
            except Exception as exc:  # noqa: BLE001 — record, then abort the stage
                # Store the failure for the main thread AND terminate the
                # children now: a stage blocked in docker/ffmpeg must not keep
                # running after the lock is gone.
                self._background_error = exc
                log.exception("supervisor background tick failed")
                self.enforce_deadline()
                return
            self._stop.wait(poll_sec)

    def stop_background(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def enforce_deadline(self) -> None:
        """SIGTERM now, SIGKILL after the 30-60 s grace window (CONT. 8 step 8).

        The bounded sleep between the two signals is the injected sleeper, so
        tests verify the sequence without waiting.
        """
        if self._deadline_triggered:
            return
        self._deadline_triggered = True
        if self._signaler is None:
            log.warning("no signaler configured; children keep running until process exit")
            return
        self._signaler.sigterm()
        if self._kill_grace_sec > 0:
            self._sleeper(self._kill_grace_sec)
        self._signaler.sigkill()
