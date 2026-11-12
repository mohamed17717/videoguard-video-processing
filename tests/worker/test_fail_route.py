"""Regression tests for the ``failed`` webhook rule in ``_FailRoute.dispatch``.

The rule (round-2 fix): a deterministic failure must emit ``failed``
IMMEDIATELY — the message goes to the DLQ on attempt 1, so no later
attempt will ever report it, and the video would stay stuck in
"processing" forever otherwise. A transient failure still emits ``failed``
ONLY on the final attempt (CONTRACT.md section 3: it must not
permanently fail the video early).

Three end-to-end tests run the whole offline ``world`` pipeline; the
unit tests drive ``_FailRoute.dispatch`` directly for the routing
details (DLQ send, message deletion, single emit).
"""

from __future__ import annotations

from vg_pipeline.errors import PipelineError
from vg_worker.main import DETERMINISTIC_CODES, _FailRoute
from vg_worker.webhooks import WebhookEmitter

from conftest import JOB_ID, RUN_ID, WEBHOOK_SECRET_VALUE, FakeSqs, RecordingPoster

STAGE = "packaged"


def fresh_sqs() -> FakeSqs:
    """A clean fake SQS, for tests that must not share the world's queue."""
    return FakeSqs()


def run_world(world) -> str:
    """Run one full pipeline invocation with the world's collaborators."""
    collab = world.collaborators()
    try:
        from vg_worker.main import run

        return run(world.cfg, collab)
    finally:
        collab.shutdown()


def break_the_probe(world) -> None:
    """Make the validator reject the source: deterministic INPUT_CONTRACT."""
    from conftest import PROBE_DOCUMENT

    probe = dict(PROBE_DOCUMENT)
    probe["streams"] = [dict(probe["streams"][0], bits_per_raw_sample=10)] + probe["streams"][1:]
    world.validator_probe = probe


def failed_events(world_or_poster) -> list[dict]:
    bodies = world_or_poster.bodies
    return [b for b in bodies if b["stage"] == "failed"]


# ---------------------------------------------------------------------------
# End to end through run(): the exact three cases the fix is about
# ---------------------------------------------------------------------------

def test_deterministic_failure_on_attempt_1_sends_failed(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    break_the_probe(world)
    assert run_world(world) == "dlq"
    events = failed_events(world.poster)
    assert len(events) == 1
    failed = events[0]
    assert failed["retryable"] is False
    assert failed["qualifier"] == "validated"  # the stage at failure
    assert failed["payload"]["error_code"] == "INPUT_CONTRACT"
    assert failed["payload"]["stage_at_failure"] == "validated"
    assert failed["attempt"] == 1
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "INPUT_CONTRACT"


def test_transient_failure_on_attempt_1_does_not_send_failed(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    world.packager_fails = True  # MANIFEST_FAILED -> transient
    assert run_world(world) == "transient"
    assert failed_events(world.poster) == []  # NOT the final attempt: no webhook
    assert not world.sqs.queues["dlq"]  # left for a fresh attempt
    assert not world.sqs.deleted


def test_transient_failure_on_final_attempt_sends_failed(world) -> None:
    world.queue_canonical(epoch=1, attempt=world.cfg.queue.max_receive_count)
    world.packager_fails = True
    assert run_world(world) == "transient"
    events = failed_events(world.poster)
    assert len(events) == 1
    failed = events[0]
    assert failed["retryable"] is False
    assert failed["qualifier"] == "packaged"
    assert failed["payload"]["error_code"] == "MANIFEST_FAILED"
    assert failed["payload"]["stage_at_failure"] == "packaged"
    assert not world.sqs.queues["dlq"]


# ---------------------------------------------------------------------------
# Unit level: _FailRoute.dispatch routing details
# ---------------------------------------------------------------------------

def make_route(world, attempt: int) -> "tuple[_FailRoute, RecordingPoster]":
    poster = RecordingPoster(statuses=())
    emitter = WebhookEmitter(
        job_id=JOB_ID,
        run_id=RUN_ID,
        run_epoch=1,
        attempt=attempt,
        secret=WEBHOOK_SECRET_VALUE,
        url=world.cfg.webhook.url,
        poster=poster,
        clock=world.wall,
        sleep=world.sleep,
        backoff=tuple(world.cfg.webhook.backoff),
        request_timeout_sec=world.cfg.webhook.request_timeout_sec,
        executor=None,  # synchronous: assertions are deterministic
    )
    route = _FailRoute(
        sqs=world.sqs,
        cfg=world.cfg,
        receipt="rh-unit",
        raw_body='{"raw": true}',
        attempt=attempt,
        stage=STAGE,
        emitter=emitter,
        job_id=JOB_ID,
        run_epoch=1,
    )
    return route, poster


def test_unit_deterministic_attempt_1_emits_then_dlqs(world) -> None:
    route, poster = make_route(world, attempt=1)
    assert route.dispatch(PipelineError("INPUT_CONTRACT", "bad probe"), "INPUT_CONTRACT") == "dlq"
    events = failed_events(poster)
    assert len(events) == 1
    assert events[0]["retryable"] is False
    assert events[0]["qualifier"] == STAGE == events[0]["payload"]["stage_at_failure"]
    assert world.sqs.queues["dlq"]  # routed to the DLQ on attempt 1
    assert world.sqs.deleted == [world.cfg.queue.jobs_url]  # and the message deleted


def test_unit_transient_attempt_1_emits_nothing(world) -> None:
    route, poster = make_route(world, attempt=1)
    assert route.dispatch(PipelineError("MANIFEST_FAILED", "boom"), "MANIFEST_FAILED") == "transient"
    assert poster.bodies == []
    assert not world.sqs.queues["dlq"]
    assert world.sqs.deleted == []


def test_unit_transient_final_attempt_emits_but_stays_transient(world) -> None:
    attempt = world.cfg.queue.max_receive_count
    route, poster = make_route(world, attempt=attempt)
    assert route.dispatch(PipelineError("MANIFEST_FAILED", "boom"), "MANIFEST_FAILED") == "transient"
    events = failed_events(poster)
    assert len(events) == 1
    assert events[0]["attempt"] == attempt
    assert not world.sqs.queues["dlq"]  # transient: never DLQd
    assert world.sqs.deleted == []  # and never deleted


def test_unit_deterministic_final_attempt_emits_exactly_once(world) -> None:
    """Deterministic AND final: one webhook, one DLQ send, one delete."""
    route, poster = make_route(world, attempt=world.cfg.queue.max_receive_count)
    assert "CLAMAV" in DETERMINISTIC_CODES
    assert route.dispatch(PipelineError("CLAMAV", "virus"), "CLAMAV") == "dlq"
    assert [b["stage"] for b in poster.bodies] == ["failed"]
    assert len(world.sqs.queues["dlq"]) == 1


def test_unit_missing_emitter_still_routes(world) -> None:
    """No emitter (unorderable message): routing must not crash."""
    route = _FailRoute(sqs=world.sqs, cfg=world.cfg, receipt="rh", raw_body="{}", attempt=1)
    assert route.dispatch(PipelineError("SIDECAR_INVALID", "no sidecar"), "SIDECAR_INVALID") == "dlq"
    assert world.sqs.deleted == [world.cfg.queue.jobs_url]
    transient = _FailRoute(sqs=fresh_sqs(), cfg=world.cfg, receipt="rh2", raw_body="{}", attempt=1)
    assert transient.dispatch(PipelineError("MANIFEST_FAILED", "boom"), "MANIFEST_FAILED") == "transient"
    assert not transient.sqs.queues["dlq"]  # type: ignore[attr-defined]
    assert transient.sqs.deleted == []  # type: ignore[attr-defined]
