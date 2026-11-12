"""SQS message normalisation: raw S3 event or canonical JobMessage.

The worker accepts BOTH message shapes (CONTRACT.md section 2):

1. a raw S3 event ``{"Records":[{"s3":{"object":{...}}}]}`` produced by the
   bucket notification. A raw event does not know ``run_epoch``/``run_id``,
   so those come from the sidecar ``jobs/<job_id>/job.json``;
2. a canonical ``JobMessage`` (backend requeue / reconciliation). For this
   shape the worker MUST take ``run_epoch``/``run_id`` from the message and
   never from the (older) sidecar.

Key parsing rules: S3 encodes spaces as ``+`` in event keys, so the key is
first URL-unescaped with ``urllib.parse.unquote_plus`` and then must match
``^incoming/([0-9a-f-]{36})/source\\.mp4$``. Anything else — including other
suffixes and non-``incoming`` prefixes — is rejected so the message goes to
the DLQ. ``s3:TestEvent`` is ignored, not failed.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Callable, Mapping

from pydantic import ValidationError

from vg_pipeline.contracts import JobMessage, JobSidecar
from vg_pipeline.errors import PipelineError

log_name = "vg_worker.normalize"

# The only accepted object key: incoming/<job uuid>/source.mp4. GROUP 1 is the
# job_id. Mirrors the Go rule "QueryUnescape then match" (CONTRACT.md 2).
JOB_KEY_PATTERN = re.compile(r"^incoming/([0-9a-f-]{36})/source\.mp4$")

# The payload of the health-check event S3 sends right after a notification
# is configured on a bucket.
_TEST_EVENT = "s3:TestEvent"

# S3 uses capitalised keys in the real test event (``{"Service":..., "Event":...}``);
# some fixtures and older code paths use the lowercase variants. Accept both,
# plus the per-record ``eventName`` spelling, so the health check is ignored
# instead of being routed to the DLQ.
_TEST_EVENT_KEYS: tuple[str, ...] = ("event", "Event", "eventName", "EventName")


class TestEventIgnored(Exception):
    """The message is the S3 ``s3:TestEvent`` health check.

    The worker deletes it and terminates without touching the DLQ.
    """

    # Prevent pytest from collecting this "Test*" class during discovery.
    __test__ = False


def _fail(code_where: str, why: str) -> PipelineError:
    return PipelineError("SIDECAR_INVALID", f"{code_where}: {why}")


def _event_value(obj: Mapping) -> object:
    """Return the event-name field of an S3 event object, checking both
    the lowercase and capitalised spellings S3 actually emits.
    """
    for key in _TEST_EVENT_KEYS:
        if key in obj:
            return obj.get(key)
    return None


def _is_test_event(body: Mapping) -> bool:
    """True for the S3 health-check payload.

    Real S3 sends ``{"Service": "Amazon S3", "Event": "s3:TestEvent"}``
    (capitalised keys); older fixtures and some code paths use the lowercase
    variants. A record-level ``{"Records": [{"Event": "s3:TestEvent"}]}``
    is also treated as a test event here so it never reaches the DLQ.
    """
    if _event_value(body) == _TEST_EVENT:
        return True
    service = body.get("service", body.get("Service"))
    if service == "Amazon S3":
        for key in _TEST_EVENT_KEYS:
            if body.get(key) == _TEST_EVENT:
                return True
    records = body.get("Records")
    if isinstance(records, list):
        for record in records:
            if isinstance(record, Mapping) and _event_value(record) == _TEST_EVENT:
                return True
    return False


def _first_record_key(body: Mapping) -> tuple[str, int | None]:
    """Return ``(object_key, object_size)`` of the first usable record.

    A record with no ``s3.object.key`` is skipped. Raises when no record
    carries a matching ``incoming/<uuid>/source.mp4`` object key.
    """
    records = body.get("Records")
    if not isinstance(records, list):
        raise _fail("s3-event", "message has no Records list")
    for record in records:
        if not isinstance(record, dict):
            continue
        if _event_value(record) == _TEST_EVENT:
            raise TestEventIgnored("s3 record is the s3:TestEvent health check")
        s3 = record.get("s3")
        if not isinstance(s3, dict):
            continue
        obj = s3.get("object")
        if not isinstance(obj, dict):
            continue
        raw_key = obj.get("key")
        if not isinstance(raw_key, str):
            continue
        # S3 event keys are URL-encoded with '+' for spaces (Go QueryUnescape
        # equivalent), so unescape before matching.
        key = urllib.parse.unquote_plus(raw_key)
        match = JOB_KEY_PATTERN.match(key)
        if match is None:
            continue
        size = obj.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            size = None
        return key, size
    raise _fail("s3-event", "no record matches incoming/<uuid>/source.mp4")


def _load_sidecar(sidecar_loader: Callable[[str], dict], key: str, job_id: str) -> dict:
    """Read ``jobs/<job_id>/job.json`` and return it as a dict.

    THE CONTRACT: a missing sidecar, unreadable object or invalid JSON all fail
    the job in the same way (SIDECAR_INVALID), so any loader failure is mapped
    here. The loader itself never logs the sidecar contents.
    """
    sidecar_key = f"jobs/{job_id}/job.json"
    try:
        data = sidecar_loader(sidecar_key)
    except (KeyError, LookupError, OSError, ValueError) as exc:
        raise _fail("sidecar", f"cannot read {sidecar_key}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise _fail("sidecar", f"{sidecar_key} is not a JSON object")
    return data


def _sidecar_job_message(sidecar: dict, job_id: str, size: int | None) -> JobMessage:
    """Validate one sidecar document into a :class:`JobMessage` (reason=s3)."""
    if sidecar.get("schema_version") != 1:
        raise _fail("sidecar", f"schema_version must be 1, got {sidecar.get('schema_version')!r}")
    sidecar_job_id = sidecar.get("job_id")
    if sidecar_job_id != job_id:
        raise _fail("sidecar", f"sidecar job_id {sidecar_job_id!r} does not match event job_id {job_id!r}")
    if size is not None and sidecar.get("size_bytes") != size:
        # The event size must equal the size declared by the backend when the
        # sidecar was written; otherwise the upload was replaced or truncated.
        raise _fail("sidecar", f"sidecar size_bytes {sidecar.get('size_bytes')!r} != event size {size!r}")
    try:
        return JobMessage.model_validate({**sidecar, "reason": "s3"})
    except ValidationError as exc:
        raise _fail("sidecar", f"sidecar does not match the job schema: {exc.error_count()} invalid field(s)") from exc


def _canonical_job_message(body: Mapping) -> JobMessage:
    """Validate a canonical JobMessage sent directly (requeue/reconciliation)."""
    try:
        return JobMessage.model_validate(dict(body))
    except ValidationError as exc:
        raise _fail(
            "job-message",
            f"message does not match the JobMessage schema ({exc.error_count()} invalid field(s))",
        ) from exc


def normalize_message(body: object, sidecar_loader: Callable[[str], dict]) -> JobMessage:
    """Normalise one SQS message body into a :class:`JobMessage`.

    ``sidecar_loader`` fetches ``jobs/<job_id>/job.json`` from S3 and returns
    the parsed dict. It is ONLY called for raw S3 events; a canonical
    JobMessage carries its own ``run_epoch``/``run_id`` and the sidecar is
    never consulted for it.

    Raises:

    * :class:`TestEventIgnored` — the S3 health check; not a failure.
    * :class:`PipelineError` with code ``SIDECAR_INVALID`` — for a message the
      worker cannot resolve to a trustworthy job. The caller routes those to
      the DLQ (deterministic failure).
    """
    if not isinstance(body, dict):
        raise _fail("body", f"message body is not a JSON object ({type(body).__name__})")
    if _is_test_event(body):
        raise TestEventIgnored("s3:TestEvent health check")
    if "Records" in body:
        key, size = _first_record_key(body)
        if size is None:
            # Fail closed: without the event size the sidecar cannot be
            # verified at all (CONTRACT.md section 1).
            raise _fail("s3-event", "the s3 event carries no object size")
        job_id = JOB_KEY_PATTERN.match(key).group(1)  # type: ignore[union-attr]
        sidecar = _load_sidecar(sidecar_loader, key, job_id)
        return _sidecar_job_message(sidecar, job_id, size)
    return _canonical_job_message(body)


def sidecar_dict_from_bytes(raw: bytes) -> dict:
    """Parse sidecar bytes; raises ``ValueError`` for invalid JSON."""
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"job.json is not valid UTF-8 JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError("job.json is not a JSON object")
    return data


def job_sidecar_from_model(message: JobMessage) -> dict:
    """The sidecar-shaped dict of a JobMessage, order-stable (helper)."""
    return JobSidecar.model_validate(message.model_dump()).model_dump()
