"""The full run() success path, failure routing, lock + redelivery flows.

Fully offline: docker is a script-writing fake, ffmpeg/the packager never
run, and every POST goes to a recorder. The REAL vg_pipeline logic (probe
parsing, input contract, canonical manifests, the aligned-timeline gate)
executes for real.
"""

from __future__ import annotations

import json
from pathlib import Path

from vg_worker.main import run

from conftest import JOB_ID, SOURCE_BYTES, dict_to_bytes

INCOMING = f"incoming/{JOB_ID}/source.mp4"
QUALITIES = ("720p", "480p", "360p", "240p")


def run_world(world) -> str:
    """Run one full pipeline invocation with the world's collaborators."""
    collab = world.collaborators()
    try:
        return run(world.cfg, collab)
    finally:
        collab.shutdown()


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_full_success_run(world) -> None:
    world.queue_canonical(epoch=11, attempt=1)
    outcome = run_world(world)
    assert outcome == "done"

    # -- masters + reports published with per-retention storage class ----
    assert world.s3.has(f"masters/{JOB_ID}/variant_a.mp4")
    assert world.s3.has(f"masters/{JOB_ID}/variant_b.mp4")
    assert world.s3.objects[f"masters/{JOB_ID}/variant_a.mp4"].storage_class == "GLACIER_IR"
    assert world.s3.has(f"reports/{JOB_ID}/config.json")
    assert world.s3.has(f"reports/{JOB_ID}/generation_report.json")
    assert world.s3.objects[f"reports/{JOB_ID}/config.json"].storage_class == "STANDARD"

    # -- renditions: ONE canonical init per quality shared by a+b --------
    for quality in QUALITIES:
        assert world.s3.has(f"renditions/{JOB_ID}/init/{quality}/init.mp4")
        for variant in ("a", "b"):
            assert world.s3.has(f"renditions/{JOB_ID}/{variant}/{quality}/seg_00001.m4s")
            assert world.s3.has(f"renditions/{JOB_ID}/{variant}/{quality}/seg_00002.m4s")
    a_seg = world.s3.objects[f"renditions/{JOB_ID}/a/720p/seg_00001.m4s"]
    assert a_seg.storage_class == "GLACIER_IR"
    assert f"vg-retention=archive" in a_seg.tagging

    # -- shared audio tree + thumbnails + canonical manifests ------------
    assert world.s3.has(f"renditions/{JOB_ID}/audio/init.mp4")
    assert world.s3.has(f"renditions/{JOB_ID}/audio/seg_00001.m4s")
    assert world.s3.has(f"renditions/{JOB_ID}/thumbs/thumb_01.jpg")
    assert world.s3.has(f"renditions/{JOB_ID}/hls/master.m3u8")
    assert world.s3.has(f"renditions/{JOB_ID}/dash/stream.mpd")

    # -- the HEAD check ran for both variants of quality 720p ------------
    heads = [k for op, k in world.s3.events if op == "head"]
    assert f"renditions/{JOB_ID}/a/720p/seg_00001.m4s" in heads
    assert f"renditions/{JOB_ID}/b/720p/seg_00001.m4s" in heads

    # -- the original got processed=true (archive) ------------------------
    tag_events = [k for op, k in world.s3.events if op == "tag"]
    assert INCOMING in tag_events
    tag_set = json.loads(world.s3.objects[INCOMING].tagging)
    tags = {t["Key"]: t["Value"] for t in tag_set["TagSet"]}
    assert tags["processed"] == "true"
    assert tags["vg-retention"] == "archive"

    # -- till the very end: message deleted, DLQ untouched, terminated ---
    assert world.sqs.deleted == [world.cfg.queue.jobs_url]
    assert not world.sqs.queues["dlq"]
    assert len(world.asg.calls) == 1
    assert world.asg.calls[0]["ShouldDecrementDesiredCapacity"] is True

    # -- webhook stage order, monotonic seq, per-quality qualifiers ------
    stages_seen = [(s, q) for s, q, _seq in world.poster.stages]
    expected_renditions = [("rendition_ready", q) for q in QUALITIES]
    assert stages_seen == (
        [("validated", ""), ("masters_ready", ""), *expected_renditions, ("packaged", ""), ("done", "")]
    )
    seqs = [s for _s, _q, s in world.poster.stages]
    assert seqs == list(range(1, len(seqs) + 1))
    for _url, _headers, body in world.poster.calls:
        event = json.loads(body)
        assert event["run_epoch"] == 11
        assert event["attempt"] == 1
        assert event["job_id"] == JOB_ID
    rendition_events = [b for b in world.poster.bodies if b["stage"] == "rendition_ready"]
    assert [b["payload"]["quality"] for b in rendition_events] == list(QUALITIES)
    assert all(set(b["payload"]["bytes"]) == {"a", "b"} for b in rendition_events)

    packaged = [b for b in world.poster.bodies if b["stage"] == "packaged"][-1]
    assert packaged["payload"]["hls_key"] == f"renditions/{JOB_ID}/hls/master.m3u8"
    assert packaged["payload"]["dash_key"] == f"renditions/{JOB_ID}/dash/stream.mpd"
    assert packaged["payload"]["segment_counts"] == {q: 2 for q in QUALITIES}
    assert packaged["payload"]["aligned"] == {"init_identical": True, "tfdt_aligned": True}

    # -- _DONE contents (CONTRACT.md section 3) and manifest.json ---------
    done = json.loads(world.s3.objects[f"reports/{JOB_ID}/_DONE"].data)
    assert done["job_id"] == JOB_ID
    assert done["manifest_key"] == f"reports/{JOB_ID}/manifest.json"
    assert done["webhooks_ok"] is True
    assert done["attempt"] == 1
    payload = done["done_payload"]
    assert payload["qualities"] == list(QUALITIES)
    assert payload["hls_key"] == f"renditions/{JOB_ID}/hls/master.m3u8"
    assert payload["dash_key"] == f"renditions/{JOB_ID}/dash/stream.mpd"
    assert payload["retention"] == "archive"
    assert payload["watermark_secret_version"] == "wmv7"
    assert payload["sha256"] == _sha256_hex(SOURCE_BYTES)
    assert payload["storage_bytes"] > 0

    master = json.loads(world.s3.objects[f"reports/{JOB_ID}/manifest.json"].data)
    assert master["qualities"] == list(QUALITIES)
    assert master["source_sha256"] == payload["sha256"]
    assert master["key_id"] .startswith("00000000")
    # never the content key or IV
    blob = dict_to_bytes(master)
    assert b"AQIDBAUGBwgJCgsMDQ4PEA==" not in blob
    assert b"AgMEBQYHCAkKCwwNDg8QEQ==" not in blob


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_worker_never_polls_a_second_message(world) -> None:
    """Exactly one receive even when the queue holds a second message."""
    world.queue_canonical(epoch=1)
    world.queue_canonical(epoch=2)
    run_world(world)
    assert world.sqs.received == 1


def test_wm_secret_file_removed_after_run(world) -> None:
    world.queue_canonical(epoch=1)
    run_world(world)
    secret_dir = Path(world.cfg.secret_file_dir)
    assert list(secret_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Normalisation / routing failures (DLQ + test events)
# ---------------------------------------------------------------------------

def test_queue_empty_terminates_without_work(world) -> None:
    outcome = run_world(world)
    assert outcome == "no-message"
    assert len(world.asg.calls) == 1
    assert world.poster.calls == []


def test_test_event_is_deleted_not_dlqd(world) -> None:
    world.sqs.queues["jobs"].append(
        {"MessageId": "m", "Body": json.dumps({"service": "Amazon S3", "event": "s3:TestEvent"}),
         "ReceiptHandle": "rh", "Attributes": {"ApproximateReceiveCount": "1"}}
    )
    assert run_world(world) == "test-event"
    assert world.sqs.deleted and not world.sqs.queues["dlq"]
    assert len(world.asg.calls) == 1  # the finally block still terminates (IMDS lookup)


# ---------------------------------------------------------------------------
# Termination on EVERY exit path (CONTRACT.md 8: no leaked g6.xlarge)
# ---------------------------------------------------------------------------

def test_test_event_path_terminates_instance(world) -> None:
    """The s3:TestEvent early return still ends with self-termination."""
    world.sqs.queues["jobs"].append(
        {"MessageId": "m", "Body": json.dumps({"service": "Amazon S3", "event": "s3:TestEvent"}),
         "ReceiptHandle": "rh", "Attributes": {"ApproximateReceiveCount": "1"}}
    )
    assert run_world(world) == "test-event"
    (call,) = world.asg.calls
    assert call["InstanceId"] == world.imds.instance_id  # resolved via IMDS
    assert call["ShouldDecrementDesiredCapacity"] is True


def test_normalize_pipeline_error_path_terminates_instance(world) -> None:
    """A SIDECAR_INVALID DLQ routing still ends with self-termination."""
    world.queue_raw_event(key=f"incoming/{JOB_ID}/source.mov")  # wrong suffix
    assert run_world(world) == "dlq"
    (call,) = world.asg.calls
    assert call["InstanceId"] == world.imds.instance_id
    assert call["ShouldDecrementDesiredCapacity"] is True


def test_imds_failure_path_still_attempts_termination(world) -> None:
    """IMDS down before the instance id is known: the finally block must
    still ATTEMPT the IMDS-based terminate, and no exception may escape."""
    world.queue_canonical(epoch=1)

    calls: list[tuple[str, str]] = []

    def failing_imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        calls.append((method, path))
        return 404, b""

    collab = world.collaborators()
    collab.imds = failing_imds
    try:
        outcome = run(world.cfg, collab)
    finally:
        collab.shutdown()
    assert outcome == "transient"  # the finally block swallowed the IMDS error
    # The in-flow identity fetch failed once; the finally block tried AGAIN.
    assert len(calls) >= 2
    assert not world.asg.calls  # IMDS kept failing: zero ASG calls is OK


def test_raw_event_success_flow(world) -> None:
    """A RAW s3 event resolves the job through the sidecar."""
    world.queue_raw_event()  # preloads incoming + jobs/<id>/job.json
    assert run_world(world) == "done"


def test_malformed_key_goes_to_dlq_immediately(world) -> None:
    world.queue_raw_event(key="incoming/" + JOB_ID + "/source.mov")
    assert run_world(world) == "dlq"
    assert world.sqs.deleted == [world.cfg.queue.jobs_url]
    dlq = world.dlq_messages()
    assert len(dlq) == 1
    assert dlq[0]["error"]["code"] == "SIDECAR_INVALID"
    assert dlq[0]["original"]["Records"]  # original body is preserved
    assert world.poster.calls == []  # no orderable event: no webhook


def test_missing_sidecar_goes_to_dlq(world) -> None:
    """The raw event without a sidecar goes straight to the DLQ."""
    world.queue_raw_event(preload=False)
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "SIDECAR_INVALID"
    assert world.sqs.deleted == [world.cfg.queue.jobs_url]  # deleted after the DLQ send


def test_size_mismatch_goes_to_dlq(world) -> None:
    world.queue_raw_event(size=12345)  # sidecar says 64, event says 12345
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "SIDECAR_INVALID"


def test_canonical_message_never_reads_sidecar(world) -> None:
    world.queue_canonical(epoch=3)
    assert not world.s3.has(f"jobs/{JOB_ID}/job.json")  # proves loader is never invoked
    assert run_world(world) == "done"


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

def preload_lock(world, expires_at: float) -> str:
    body = dict_to_bytes({"instance_id": "i-0other", "attempt": 1, "expires_at": expires_at})
    return world.s3.put(f"reports/{JOB_ID}/_LOCK", body)


def test_lock_busy_leaves_message_visible(world) -> None:
    world.queue_canonical(epoch=2)
    preload_lock(world, expires_at=world.wall.value + 600)
    outcome = run_world(world)
    assert outcome == "busy"
    # A busy lock is NOT proof that another worker will finish: deleting the
    # message permanently lost the job when the lock belonged to a failed run.
    assert not world.sqs.deleted
    assert not world.s3.has(f"masters/{JOB_ID}/variant_a.mp4")
    assert len(world.asg.calls) == 1  # still terminates afterwards


def test_lock_takeover_after_expiry(world) -> None:
    world.queue_canonical(epoch=2)
    preload_lock(world, expires_at=world.wall.value - 10)  # expired
    assert run_world(world) == "done"


# ---------------------------------------------------------------------------
# Deadline, disk, transient infra
# ---------------------------------------------------------------------------

def test_deadline_exceeded_is_transient(world) -> None:
    world.queue_canonical(epoch=1)
    world.reconfigure(**{"deadlines.job_sec": 301})
    runner = world.container_runner()

    def slow_validator(argv: list[str]):
        world.monotonic.advance(400.0)  # past the 301 s deadline
        return runner(argv)

    collab = world.collaborators()
    collab.container_runner = slow_validator
    try:
        outcome = run(world.cfg, collab)
    finally:
        collab.shutdown()
    assert outcome == "transient"
    assert not world.sqs.deleted  # message stays for a fresh attempt
    assert not world.sqs.queues["dlq"]
    assert world.signaler.signals == ["TERM", "KILL"]  # supervisor escalation


def test_disk_full_goes_to_dlq(world) -> None:
    world.queue_canonical(epoch=1)
    world.disk_free_value = 1024
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "DISK_FULL"


def test_secret_fetch_failure_is_transient(world) -> None:
    world.queue_canonical(epoch=1)
    world.secret_failure = RuntimeError("secretsmanager down")
    outcome = run_world(world)
    assert outcome == "transient"
    assert not world.sqs.deleted
    assert not world.sqs.queues["dlq"]
    assert world.poster.calls == []


def test_imds_failure_leaves_message_visible(world) -> None:
    """Without an instance id the box cannot ASG-terminate; leave the message."""
    world.queue_canonical(epoch=1)

    def failing_imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        return 404, b""

    collab = world.collaborators()
    collab.imds = failing_imds
    try:
        outcome = run(world.cfg, collab)
    finally:
        collab.shutdown()
    assert outcome == "transient"
    assert not world.sqs.deleted
    assert world.asg.calls == []
    assert world.poster.calls == []


def test_lock_renewal_conflict_is_transient(world) -> None:
    """The ETag changed under us -> abort, leave the message (CONTRACT.md 2)."""
    world.queue_canonical(epoch=1)
    world.reconfigure(**{"deadlines.job_sec": 301})
    runner = world.container_runner()

    def slow_validator(argv: list[str]) -> object:
        # like another worker instance taking over the expired lock
        world.s3.put(
            f"reports/{JOB_ID}/_LOCK",
            dict_to_bytes({"instance_id": "i-0thief", "attempt": 1, "expires_at": world.wall.value + 600}),
        )
        world.monotonic.advance(400.0)  # fires the first heartbeat (lock renewal)
        return runner(argv)

    collab = world.collaborators()
    collab.container_runner = slow_validator
    try:
        outcome = run(world.cfg, collab)
    finally:
        collab.shutdown()
    assert outcome == "transient"
    assert not world.sqs.deleted
    assert not world.sqs.queues["dlq"]
    stages = [b["stage"] for b in world.poster.bodies]
    assert "failed" not in stages  # non-final attempt: no failed webhook
    assert "masters_ready" not in stages  # the job aborted right after "validated"


# ---------------------------------------------------------------------------
# Stage failures: deterministic vs transient, final-attempt webhook
# ---------------------------------------------------------------------------

def _break_the_probe(world) -> None:
    from conftest import PROBE_DOCUMENT

    probe = dict(PROBE_DOCUMENT)
    probe["streams"] = [dict(probe["streams"][0], bits_per_raw_sample=10)] + probe["streams"][1:]
    world.validator_probe = probe


def test_input_contract_failure_is_deterministic(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    _break_the_probe(world)
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "INPUT_CONTRACT"
    # deterministic: the job goes to the DLQ NOW, so the `failed` webhook
    # fires on attempt 1 already (no later attempt would ever report it).
    failed = [b for b in world.poster.bodies if b["stage"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["retryable"] is False
    assert failed[0]["qualifier"] == "validated"
    assert failed[0]["payload"]["stage_at_failure"] == "validated"
    assert failed[0]["attempt"] == 1


def test_final_attempt_failed_webhook_with_qualifier(world) -> None:
    world.queue_canonical(epoch=1, attempt=3)
    _break_the_probe(world)
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "INPUT_CONTRACT"
    events = [b for b in world.poster.bodies]
    assert [b["stage"] for b in events] == ["failed"]
    failed = events[0]
    assert failed["qualifier"] == "validated"  # stage at failure
    assert failed["retryable"] is False
    assert failed["payload"]["error_code"] == "INPUT_CONTRACT"
    assert failed["payload"]["stage_at_failure"] == "validated"
    assert failed["attempt"] == 3


def test_transient_packager_failure_leaves_message_until_final(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    world.packager_fails = True
    assert run_world(world) == "transient"
    assert not world.sqs.deleted
    assert not world.sqs.queues["dlq"]
    stages = [b["stage"] for b in world.poster.bodies]
    assert "failed" not in stages  # non-final attempt: no failed webhook


def test_transient_failure_on_final_attempt_webhooks_failed(world) -> None:
    world.queue_canonical(epoch=1, attempt=3)
    world.packager_fails = True  # MANIFEST_FAILED -> transient
    assert run_world(world) == "transient"
    assert not world.sqs.deleted
    assert not world.sqs.queues["dlq"]
    stages = [b["stage"] for b in world.poster.bodies]
    # The packager fails BEFORE any rendition is published, so no
    # rendition_ready events exist (the publish happens after the gate).
    assert stages == ["validated", "masters_ready", "failed"]
    failed = world.poster.bodies[-1]
    assert failed["qualifier"] == "packaged"
    assert failed["payload"]["error_code"] == "MANIFEST_FAILED"
    assert failed["retryable"] is False
    assert failed["payload"]["stage_at_failure"] == "packaged"


def test_aligned_gate_failure_is_deterministic(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    world.packager_uneven_tfdt = "b"  # b-tree decode times drift
    assert run_world(world) == "dlq"
    (message,) = world.dlq_messages()
    assert message["error"]["code"] == "PACKAGING_INIT_MISMATCH"


# ---------------------------------------------------------------------------
# Redelivery idempotency
# ---------------------------------------------------------------------------

def test_redelivery_reposts_done_idempotently(world) -> None:
    world.queue_canonical(epoch=42, attempt=1)
    stored_payload = {
        "qualities": list(QUALITIES),
        "hls_key": f"renditions/{JOB_ID}/hls/master.m3u8",
        "dash_key": f"renditions/{JOB_ID}/dash/stream.mpd",
        "retention": "archive",
    }
    world.s3.put(
        f"reports/{JOB_ID}/_DONE",
        dict_to_bytes({"job_id": JOB_ID, "run_epoch": 42,
                       "manifest_key": f"reports/{JOB_ID}/manifest.json",
                       "webhooks_ok": False, "attempt": 1, "done_payload": stored_payload}),
    )
    assert run_world(world) == "idempotent"
    done_posts = [b for b in world.poster.bodies if b["stage"] == "done"]
    assert len(done_posts) == 1
    assert done_posts[0]["payload"] == stored_payload
    assert done_posts[0]["run_epoch"] == 42
    assert world.sqs.deleted  # and the message is gone
    assert world.asg.calls  # the instance still terminates


def test_redelivery_with_unreadable_marker_still_deletes(world) -> None:
    world.queue_canonical(epoch=1)
    world.s3.put(f"reports/{JOB_ID}/_DONE", b"not-json")
    assert run_world(world) == "idempotent"  # best effort, no partial state
    assert world.sqs.deleted


# ---------------------------------------------------------------------------
# DLQ envelope sanity
# ---------------------------------------------------------------------------

def test_dlq_envelope_carries_original_and_error() -> None:
    from vg_worker.main import _dlq_envelope

    body = _dlq_envelope('{"Records": []}', "SIDECAR_INVALID", "no record", "receive", JOB_ID, None, 2)
    parsed = json.loads(body)
    assert parsed["original"] == {"Records": []}
    assert parsed["error"]["code"] == "SIDECAR_INVALID"
    assert parsed["error"]["attempt"] == 2
    unparseable = _dlq_envelope("definitely not json", "INTERNAL_ERROR", "x", "validated", JOB_ID, 5, 1)
    assert json.loads(unparseable)["original"] == {"raw": "definitely not json"}


def test_transient_failure_expires_lock_for_retry(world) -> None:
    world.queue_canonical(epoch=1, attempt=1)
    world.packager_fails = True
    assert run_world(world) == "transient"
    stored = json.loads(world.s3.objects[f"reports/{JOB_ID}/_LOCK"].data)
    assert stored["expires_at"] == 0


def test_receive_message_requests_receive_count(world) -> None:
    world.queue_canonical(epoch=1)
    run_world(world)
    assert world.sqs.attribute_names
    assert "ApproximateReceiveCount" in world.sqs.attribute_names[0]


def test_old_epoch_done_marker_does_not_short_circuit(world) -> None:
    world.queue_canonical(epoch=2)
    world.s3.put(
        f"reports/{JOB_ID}/_DONE",
        dict_to_bytes({"job_id": JOB_ID, "run_epoch": 1, "attempt": 1, "done_payload": {}}),
    )
    assert run_world(world) == "done"
