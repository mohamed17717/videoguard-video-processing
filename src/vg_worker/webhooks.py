"""Signed progress webhooks to the backend (CONTRACT.md section 3).

One short POST per stage. Signature input is exactly
``f"{ts}.{job_id}.{stage}.{seq}.{raw_body}"`` (HMAC-SHA256, prefix
``sha256=``) — the same rule as ``internal/awscontrol/webhook_sign.go``.
The backend rejects anything older/newer than 300 s, so each retry signs
with a FRESH timestamp.

Events are ordered by ``(run_epoch, attempt, seq)`` on the backend:
``seq`` restarts at 1 on each worker attempt and is monotonically
increasing within this process. Delivery is fire-and-forget (never blocks
GPU work), with 3 retries at 1/2/4 s backoff, and :meth:`flush` provides
the bounded 5 s wait for the final ``done`` webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future, wait as wait_futures
from typing import Any, Protocol, Union

from pydantic import ValidationError

from vg_pipeline.contracts import WebhookEvent
from vg_pipeline.logging import get_logger

log = get_logger("vg_worker.webhooks")

AnySleep = Callable[[float], None]
AnyClock = Callable[[], float]
ByteBody = Union[bytes, str]

# Backend replay window (CONTRACT.md section 3): reject |now - ts| > 300 s.
MAX_CLOCK_SKEW_SEC = 300.0

# Default retry schedule: initial attempt + 3 retries with 1/2/4 s backoff.
DEFAULT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)

# Bounded wait for the final webhook before the instance terminates.
DEFAULT_FLUSH_TIMEOUT_SEC = 5.0


def sign_body(
    secret: str,
    ts: int,
    job_id: str,
    stage: str,
    seq: int,
    raw_body: ByteBody,
) -> str:
    """``sha256=<hex HMAC_SHA256(secret, "{ts}.{job_id}.{stage}.{seq}.{raw_body}")>``.

    ``raw_body`` must be the EXACT body that will be POSTed (Go signs the raw
    body string). A ``bytes`` body is signed as its UTF-8 text.
    """
    if isinstance(raw_body, bytes):
        body_text = raw_body.decode("utf-8")
    else:
        body_text = raw_body
    message = f"{ts}.{job_id}.{stage}.{seq}.{body_text}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    ts: int,
    job_id: str,
    stage: str,
    seq: int,
    raw_body: ByteBody,
    signature: str,
    *,
    now: "float | None" = None,
    max_skew_sec: float = MAX_CLOCK_SKEW_SEC,
) -> None:
    """Check a signature the way the backend does; raise ``ValueError`` when bad.

    Worker-side helper (tests, debugging): the real verification happens on
    Railway. The 300 s replay window applies.
    """
    if now is None:
        now = time.time()
    if abs(float(now) - float(ts)) > max_skew_sec:
        raise ValueError(f"timestamp {ts} is outside the replay window (+/-{max_skew_sec:g}s)")
    expected = sign_body(secret, int(ts), job_id, stage, int(seq), raw_body)
    if not hmac.compare_digest(expected, signature):
        raise ValueError("signature mismatch")


class PosterCallable(Protocol):
    """Perform one webhook POST and return ``(http_status, reason)``.

    Failures are signalled either by an exception OR by a 4xx/5xx status; the
    emitter treats both the same. Transport details stay in the poster.
    """

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_sec: float,
    ) -> "tuple[int, str]": ...


class Emitter(Protocol):
    """What stages use to report progress (implemented by WebhookEmitter)."""

    def emit(
        self,
        stage: str,
        qualifier: str = "",
        payload: "Mapping[str, Any] | None" = None,
        retryable: bool = False,
    ) -> None: ...

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT_SEC) -> None: ...


class WebhookEmitter:
    """Emit signed webhook events; fire-and-forget, never blocks the pipeline.

    Production injects a small ``ThreadPoolExecutor`` so a slow Railway never
    holds the GPU box; offline tests pass ``executor=None`` and get fully
    synchronous behaviour they can assert on.
    """

    def __init__(
        self,
        *,
        job_id: str,
        run_id: str,
        run_epoch: int,
        attempt: int,
        secret: str,
        url: str,
        poster: PosterCallable,
        clock: "AnyClock | None" = None,
        sleep: "AnySleep | None" = None,
        backoff: "Sequence[float]" = DEFAULT_BACKOFF,
        request_timeout_sec: float = 5.0,
        executor: "Executor | None" = None,
    ) -> None:
        if not secret or not url or not job_id:
            raise ValueError("WebhookEmitter needs job_id, secret and url")
        self._job_id = job_id
        self._run_id = run_id
        self._run_epoch = int(run_epoch)
        self._attempt = int(attempt)
        self._secret = secret
        self._url = url
        self._poster = poster
        self._clock: AnyClock = clock or time.time
        self._sleep: AnySleep = sleep or time.sleep
        self._backoff: tuple[float, ...] = tuple(backoff) or DEFAULT_BACKOFF
        self._timeout = float(request_timeout_sec)
        self._executor = executor
        self._seq_lock = threading.Lock()
        self._seq_counter = itertools.count(start=1)
        # Futures of background deliveries, for the bounded flush.
        self._futures: list[Future] = []
        # Outcome of the most recent completed emit (None = nothing settled
        # yet). The finalize stage reads it for ``webhooks_ok``.
        self.last_success: "bool | None" = None

    # -- internals --------------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            return next(self._seq_counter)

    def _build_event(
        self,
        stage: str,
        qualifier: str,
        payload: "Mapping[str, Any] | None",
        retryable: bool,
        seq: int,
        *,
        ts: "int | None" = None,
    ) -> bytes:
        if ts is None:
            ts = int(self._clock())
        event = WebhookEvent(
            job_id=self._job_id,
            run_id=self._run_id,
            run_epoch=self._run_epoch,
            stage=stage,
            seq=seq,
            attempt=self._attempt,
            timestamp=int(ts),
            qualifier=qualifier,
            retryable=retryable,
            payload=dict(payload or {}),
        )
        return event.model_dump_json().encode("utf-8")

    def _headers(self, ts: int, job_id: str, stage: str, seq: int, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-VG-Timestamp": str(ts),
            "X-VG-Signature": sign_body(self._secret, ts, job_id, stage, seq, body),
        }

    def _deliver(
        self,
        stage: str,
        qualifier: str,
        payload: "Mapping[str, Any] | None",
        retryable: bool,
        seq: int,
    ) -> None:
        """All attempts for one event; every failure is logged, none raised."""
        attempts = len(self._backoff) + 1
        for index in range(attempts):
            ts = int(self._clock())
            try:
                body = self._build_event(stage, qualifier, payload, retryable, seq, ts=ts)
            except ValidationError as exc:
                log.error("webhook event invalid", extra={"stage": stage, "errors": exc.error_count()})
                self.last_success = False
                return
            headers = self._headers(ts, self._job_id, stage, seq, body)
            try:
                status, reason = self._poster(self._url, headers, body, self._timeout)
                if 200 <= status < 300:
                    self.last_success = True
                    log.debug(
                        "webhook delivered",
                        extra={"stage": stage, "qualifier": qualifier, "seq": seq, "status": status},
                    )
                    return
                outcome: str = f"http {status} {reason}"
            except Exception as exc:  # noqa: BLE001 — fire-and-forget contract
                outcome = type(exc).__name__
            if index < attempts - 1:
                delay = self._backoff[index]
                log.warning(
                    "webhook attempt failed, retrying",
                    extra={"stage": stage, "qualifier": qualifier, "seq": seq, "outcome": outcome, "in_sec": delay},
                )
                self._sleep(delay)
        # Every attempt failed. Files still land in S3; the backend can
        # rebuild status from reports/ + renditions/ (reconciliation).
        self.last_success = False
        log.error(
            "webhook failed after all retries",
            extra={"stage": stage, "qualifier": qualifier, "seq": seq},
        )

    # -- public API ---------------------------------------------------------

    def emit(
        self,
        stage: str,
        qualifier: str = "",
        payload: "Mapping[str, Any] | None" = None,
        retryable: bool = False,
    ) -> None:
        """Queue one signed event; monotonic ``seq`` starting at 1 per attempt.

        Never raises and never returns the posting result — the pipeline must
        not block on Railway (CONTRACT.md section 3, fire-and-forget).
        """
        seq = self._next_seq()
        if self._executor is None:
            self._deliver(stage, qualifier, payload, retryable, seq)
            return
        future = self._executor.submit(self._deliver, stage, qualifier, payload, retryable, seq)
        self._futures.append(future)

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT_SEC) -> None:
        """Bounded wait for queued events to finish (final ``done`` webhook).

        With no background executor there is nothing pending, so this returns
        immediately (bounded by construction).
        """
        if self._executor is None:
            return
        wait_futures([f for f in self._futures if not f.done()], timeout=timeout)
