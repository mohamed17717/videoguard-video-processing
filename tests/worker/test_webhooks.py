"""HMAC signatures, replay window, seq monotonicity, retries, flush."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from vg_worker.webhooks import (
    DEFAULT_BACKOFF,
    DEFAULT_FLUSH_TIMEOUT_SEC,
    WebhookEmitter,
    sign_body,
    verify_signature,
)

from conftest import JOB_ID, RecordingPoster, RUN_ID, SleepRecorder

SECRET = "fixed-test-webhook-secret"


def test_sign_body_matches_go_formula() -> None:
    """``sha256=HMAC(secret, f"{ts}.{job_id}.{stage}.{seq}." + raw_body)``."""
    import hmac
    import hashlib

    body = '{"schema_version":1,"seq":1}'
    expected = (
        "sha256="
        + hmac.new(
            SECRET.encode(),
            f"1730000000.{JOB_ID}.validated.1.{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    assert sign_body(SECRET, 1730000000, JOB_ID, "validated", 1, body) == expected


def test_verify_round_trip_and_tamper() -> None:
    body = json.dumps({"schema_version": 1}).encode()
    sig = sign_body(SECRET, 1_000, JOB_ID, "done", 3, body)
    verify_signature(SECRET, 1_000, JOB_ID, "done", 3, body, sig, now=1_050)
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_signature(SECRET, 1_000, JOB_ID, "done", 3, b'{"x":2}', sig, now=1_050)
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_signature("wrong-secret", 1_000, JOB_ID, "done", 3, body, sig, now=1_050)


def test_replay_window_rejects_old_timestamps() -> None:
    body = b"{}"
    sig = sign_body(SECRET, 1_000, JOB_ID, "done", 1, body)
    verify_signature(SECRET, 1_000, JOB_ID, "done", 1, body, sig, now=1_299)
    with pytest.raises(ValueError, match="replay window"):
        verify_signature(SECRET, 1_000, JOB_ID, "done", 1, body, sig, now=1_301)


def test_emitter_retries_with_backoff_and_fresh_signatures() -> None:
    poster = RecordingPoster(statuses=[500, 503, 502, 200])
    sleep = SleepRecorder()
    tamper_clock = json_compatible_sequence()
    emitter = WebhookEmitter(
        job_id=JOB_ID,
        run_id=RUN_ID,
        run_epoch=7,
        attempt=1,
        secret=SECRET,
        url="https://railway.example.invalid/webhook",
        poster=poster,
        clock=tamper_clock,
        sleep=sleep,
    )
    emitter.emit("validated", payload={"payload_ok": True})
    # 1 initial attempt + 3 retries, sleeps 1/2/4 between them.
    assert len(poster.calls) == 4
    assert list(sleep.sleeps) == [1.0, 2.0, 4.0]
    assert emitter.last_success is True
    for url, headers, body in poster.calls:
        assert url == "https://railway.example.invalid/webhook"
        assert headers["Content-Type"] == "application/json"
        ts = int(headers["X-VG-Timestamp"])
        verify_signature(SECRET, ts, JOB_ID, "validated", 1, body, headers["X-VG-Signature"], now=ts)
    event = poster.bodies[-1]
    assert event["job_id"] == JOB_ID
    assert event["run_epoch"] == 7
    assert event["attempt"] == 1
    assert event["seq"] == 1
    assert event["stage"] == "validated"
    assert event["payload"] == {"payload_ok": True}


def json_compatible_sequence() -> "object":
    """A mono-increasing clock with plausible unix-second values."""
    import itertools

    seconds = itertools.count(start=1_000_000)

    def clock() -> float:
        return float(next(seconds))

    return clock


def test_seq_is_monotonic_per_attempt() -> None:
    poster = RecordingPoster(statuses=())
    emitter = WebhookEmitter(
        job_id=JOB_ID, run_id=RUN_ID, run_epoch=1, attempt=2, secret=SECRET,
        url="https://railway.example.invalid/h", poster=poster,
        clock=json_compatible_sequence(), sleep=SleepRecorder(),
    )
    emitter.emit("validated")
    emitter.emit("masters_ready")
    emitter.emit("rendition_ready", qualifier="720p")
    emitter.emit("rendition_ready", qualifier="480p")
    stages_seen = [(b["stage"], b["qualifier"], b["seq"]) for b in poster.bodies]
    assert stages_seen == [
        ("validated", "", 1),
        ("masters_ready", "", 2),
        ("rendition_ready", "720p", 3),
        ("rendition_ready", "480p", 4),
    ]


def test_emit_never_raises_on_transport_failure() -> None:
    poster = RecordingPoster(statuses=())
    poster.fail_all = True
    emitter = WebhookEmitter(
        job_id=JOB_ID, run_id=RUN_ID, run_epoch=1, attempt=1, secret=SECRET,
        url="https://railway.example.invalid/h", poster=poster,
        clock=json_compatible_sequence(), sleep=SleepRecorder(),
    )
    emitter.emit("packaged", payload={"hls_key": "k"})  # must not raise
    assert emitter.last_success is False
    assert len(poster.calls) == len(DEFAULT_BACKOFF) + 1  # all attempts attempted


def test_flush_is_bounded_without_executor() -> None:
    poster = RecordingPoster(statuses=())
    emitter = WebhookEmitter(
        job_id=JOB_ID, run_id=RUN_ID, run_epoch=1, attempt=3, secret=SECRET,
        url="https://railway.example.invalid/h", poster=poster,
        clock=json_compatible_sequence(), sleep=SleepRecorder(),
    )
    emitter.emit("done")
    # synchronous mode: nothing pending, flush returns immediately (bounded).
    emitter.flush(timeout=0.001)
    assert poster.bodies[0]["stage"] == "done"
    assert DEFAULT_FLUSH_TIMEOUT_SEC == 5.0


def test_flush_waits_for_a_slow_background_emission() -> None:
    poster = RecordingPoster(statuses=())
    with ThreadPoolExecutor(max_workers=1) as executor:
        emitter = WebhookEmitter(
            job_id=JOB_ID, run_id=RUN_ID, run_epoch=1, attempt=1, secret=SECRET,
            url="https://railway.example.invalid/h", poster=poster,
            clock=json_compatible_sequence(), sleep=SleepRecorder(),
            executor=executor,
        )
        emitter.emit("done")
        emitter.flush(timeout=5.0)
        # the worker then shuts the executor down; emission already settled
    assert [b["stage"] for b in poster.bodies] == ["done"]
