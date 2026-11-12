"""Message normalisation: raw S3 events, canonical JobMessage, test events."""

from __future__ import annotations

import pytest

from vg_pipeline.errors import PipelineError
from vg_worker.normalize import TestEventIgnored, normalize_message

from conftest import JOB_ID, RUN_ID, SIZE_BYTES, sidecar_dict, raw_s3_event


def test_url_encoded_key_is_unescaped() -> None:
    """A percent-encoded UUID in the event key must still resolve the job.

    S3 event payloads URL-encode object keys; the worker unescapes with
    ``urllib.parse.unquote_plus`` before matching.
    """
    encoded_job = "61fdf3a2-054c-4d09-b37f-754677659021"
    encoded_key = "incoming/61fdf3a2%2D054c%2D4d09%2Db37f%2D754677659021/source.mp4"
    sidecar = sidecar_dict()
    sidecar["job_id"] = encoded_job

    def sidecar_loader(key: str) -> dict:
        assert key == f"jobs/{encoded_job}/job.json"
        return sidecar

    message = normalize_message(raw_s3_event(encoded_key, size=SIZE_BYTES), sidecar_loader)
    assert message.job_id == encoded_job
    assert message.run_epoch == 1
    assert message.reason == "s3"


def test_wrong_suffix_is_rejected() -> None:
    body = raw_s3_event("incoming/" + JOB_ID + "/source.mov", size=SIZE_BYTES)
    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, _no_sidecar)
    assert excinfo.value.code == "SIDECAR_INVALID"


def test_other_prefix_is_rejected() -> None:
    body = raw_s3_event("uploads/" + JOB_ID + "/source.mp4", size=SIZE_BYTES)
    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, _no_sidecar)
    assert excinfo.value.code == "SIDECAR_INVALID"


def test_multi_suffix_or_uppercase_rejected() -> None:
    body = raw_s3_event("incoming/" + JOB_ID + "/SOURCE.MP4", size=SIZE_BYTES)
    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, _no_sidecar)
    assert excinfo.value.code == "SIDECAR_INVALID"


def _no_sidecar(key: str) -> dict:
    raise AssertionError(f"sidecar must not be read for a rejected key ({key})")


def test_test_event_is_ignored_not_failed() -> None:
    for body in (
        {"service": "Amazon S3", "event": "s3:TestEvent"},
        {"Records": [{"event": "s3:TestEvent"}]},
    ):
        with pytest.raises(TestEventIgnored):
            normalize_message(body, _no_sidecar)


def test_missing_sidecar_fails() -> None:
    def sidecar_loader(key: str) -> dict:
        raise FileNotFoundError(key)

    body = raw_s3_event("incoming/" + JOB_ID + "/source.mp4", size=SIZE_BYTES)
    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, sidecar_loader)
    assert excinfo.value.code == "SIDECAR_INVALID"


def test_size_mismatch_fails() -> None:
    def sidecar_loader(key: str) -> dict:
        return dict(sidecar_dict(size_bytes=999))

    body = raw_s3_event("incoming/" + JOB_ID + "/source.mp4", size=SIZE_BYTES)
    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, sidecar_loader)
    assert excinfo.value.code == "SIDECAR_INVALID"


def test_sidecar_job_id_mismatch_fails() -> None:
    sidecar = sidecar_dict()
    sidecar["job_id"] = "deadbeef-0000-1111-2222-333344445555"

    def sidecar_loader(key: str) -> dict:
        return sidecar

    body = raw_s3_event("incoming/" + JOB_ID + "/source.mp4", size=SIZE_BYTES)
    with pytest.raises(PipelineError):
        normalize_message(body, sidecar_loader)


def test_raw_event_uses_sidecar_run_epoch() -> None:
    sidecar = sidecar_dict(run_epoch=4, run_id=RUN_ID)

    def sidecar_loader(key: str) -> dict:
        assert key == f"jobs/{JOB_ID}/job.json"
        return sidecar

    message = normalize_message(
        raw_s3_event("incoming/" + JOB_ID + "/source.mp4", size=SIZE_BYTES), sidecar_loader
    )
    assert message.run_epoch == 4
    assert message.run_id == RUN_ID
    assert message.reason == "s3"


def test_requeue_takes_run_epoch_from_message_not_sidecar() -> None:
    """A canonical JobMessage supersedes the sidecar: run fields from MESSAGE.

    The loader raising proves the sidecar is NEVER consulted for requeues.
    """
    message_body = dict(sidecar_dict(run_epoch=9, run_id="newer-run-uuid-0000-0000"))
    message_body["reason"] = "requeue"

    message = normalize_message(message_body, _no_sidecar)
    assert message.run_epoch == 9
    assert message.run_id == "newer-run-uuid-0000-0000"
    assert message.reason == "requeue"


def test_bad_canonical_message_fails() -> None:
    with pytest.raises(PipelineError) as excinfo:
        normalize_message({"job_id": "x"}, _no_sidecar)
    assert excinfo.value.code == "SIDECAR_INVALID"


def test_event_without_size_fails_closed() -> None:
    body = {"Records": [{"s3": {"object": {"key": "incoming/" + JOB_ID + "/source.mp4"}}}]}

    def sidecar_loader(key: str) -> dict:
        return dict(sidecar_dict())

    with pytest.raises(PipelineError) as excinfo:
        normalize_message(body, sidecar_loader)
    assert excinfo.value.code == "SIDECAR_INVALID"
