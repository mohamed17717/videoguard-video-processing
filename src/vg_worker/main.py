"""Worker entry point: wire everything into the CONTRACT.md section 8 order.

One SQS message per process lifetime:

1. receive exactly ONE message (``MaxNumberOfMessages=1``);
2. normalise it (raw S3 event -> sidecar, or canonical JobMessage);
3. identify the instance via IMDSv2 and fetch the three secrets;
4. on a redelivery where ``_DONE`` exists, re-POST ``done`` (idempotent);
5. check the disk headroom gate (2.5x source + 20 GiB);
6. acquire ``reports/<job>/_LOCK`` with a conditional write (on 412 take
   over ONLY when the stored lock has expired);
7. run the stages under the deadline supervisor; every 300 s the heartbeat
   refreshes the SQS visibility timeout and renews the lock;
8. ``done`` webhook -> write ``_DONE`` AFTER the bounded POST -> delete;
9. on failure: ``failed`` webhook when the failure is DETERMINISTIC or it
   is the FINAL attempt (qualifier is the stage at failure), deterministic
   codes go to the DLQ IMMEDIATELY, transient failures leave the message
   for a redelivery;
10. terminate the instance (ASG call with ShouldDecrementDesiredCapacity,
    then the ``shutdown -h now`` fallback).

All side effects (boto3, subprocess, HTTP, docker, filesystem) go through
:class:`Collaborators`, so tests run fully offline with fakes.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Union

from vg_pipeline.contracts import AxinomSecrets, JobMessage, PipelineConfig, load_pipeline_config
from vg_pipeline.errors import PipelineError
from vg_pipeline.logging import configure_logging, get_logger

from vg_worker import stages
from vg_worker import supervisor as supervisor_mod
from vg_worker import terminate as terminate_mod
from vg_worker.config import WorkerConfig, load_worker_config
from vg_worker.container import ContainerError
from vg_worker.normalize import TestEventIgnored, normalize_message, sidecar_dict_from_bytes
from vg_worker.s3io import S3Client, S3IO
from vg_worker.secrets import SecretsClient, fetch_secrets, watermark_secret_file
from vg_worker.supervisor import DeadlineExceeded, ProcessGroupSignaler, Signaler, Supervisor
from vg_worker.webhooks import PosterCallable, WebhookEmitter

log = get_logger("vg_worker.main")

PathLike = Union[str, Path]
ImdsGetter = Callable[[str, str, Mapping[str, str]], "tuple[int, bytes]"]
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]

# Codes the CONTRACT.md section 2 list marks deterministic: they go to the
# DLQ on the FIRST failure, because a retry with the same input fails again.
# Every other error is transient: the message stays visible and SQS returns
# it for up to ``max_receive_count`` attempts.
DETERMINISTIC_CODES: frozenset[str] = frozenset(
    {
        "INPUT_CONTRACT",
        "CLAMAV",
        "GENERATOR_EXIT_2",
        "PACKAGING_INIT_MISMATCH",
        "SIDECAR_INVALID",
        "ENCODER_UNAVAILABLE",
        "DISK_FULL",
    }
)

# Webhook payload codes for non-PipelineError failures. They are NOT part of
# vg_pipeline.errors.ERROR_CODES on purpose: they describe infrastructure,
# not media verdicts, and they always stay transient (message left visible).
DEADLINE_CODE = "DEADLINE_EXCEEDED"
CONTAINER_CODE = "CONTAINER_ERROR"
INTERNAL_CODE = "INTERNAL_ERROR"

_LOCK_SUFFIX = "_LOCK"
_DONE_SUFFIX = "_DONE"
_CHUNK = 1_073_741_824  # GiB -> bytes, for the disk headroom gate


class SqsClient(Protocol):
    """The subset of a boto3 SQS client the worker needs."""

    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_message(self, **kwargs: Any) -> Any: ...

    def change_message_visibility(self, **kwargs: Any) -> Any: ...

    def send_message(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass
class Collaborators:
    """Every injectable side effect of the worker process.

    Production builds one with :func:`default_collaborators`; tests build one
    with fakes. NOTHING here touches the network or the filesystem until
    :func:`run` starts working.
    """

    sqs: SqsClient
    s3_factory: Callable[[], S3Client]
    secrets_client: SecretsClient
    imds: ImdsGetter
    asg: Any  # boto3 autoscaling client or a fake
    container_runner: Callable[..., Any]
    ffmpeg_runner: Callable[..., Any]
    packager_runner: Callable[..., Any]
    disk_free: Callable[[str], int]
    monotonic: ClockFn
    wall_clock: ClockFn
    sleep: SleepFn
    axinom_post: "Callable[[str, bytes], tuple[int, bytes]] | None" = None
    poster: "PosterCallable | None" = None
    webhook_executor: "Executor | None" = None
    signaler: "Signaler | None" = None

    def shutdown(self) -> None:
        """Stop the background webhook threads, if any."""
        if self.webhook_executor is not None:
            self.webhook_executor.shutdown(wait=False)
            self.webhook_executor = None


@dataclass
class LockState:
    """Our current hold on ``reports/<job>/_LOCK``."""

    key: str
    etag: "str | None"
    expires_at: int


def _lock_body(instance_id_value: str, attempt: int, expires_at: int) -> bytes:
    """The ``_LOCK`` payload: ``{instance_id, attempt, expires_at}``."""
    return json.dumps(
        {"instance_id": instance_id_value, "attempt": attempt, "expires_at": expires_at},
        sort_keys=True,
    ).encode("utf-8")


def acquire_lock(
    s3: S3IO,
    lock_key: str,
    instance_id_value: str,
    attempt: int,
    ttl_sec: float,
    now: float,
) -> "LockState | None":
    """Take ``_LOCK`` with a conditional write; ``None`` means another owner.

    On a 412 the existing lock is READ: a still-future ``expires_at`` means
    another worker instance is live and this message is dropped. An EXPIRED
    lock is taken over with a conditional write on the OLD ETag; a lost race
    returns ``None`` again.
    """
    expires_at = int(now + ttl_sec)
    ok, etag = s3.conditional_lock(lock_key, _lock_body(instance_id_value, attempt, expires_at))
    if ok:
        return LockState(key=lock_key, etag=etag, expires_at=expires_at)
    try:
        raw_lock, old_etag = s3.get_object_with_etag(lock_key)
        current = json.loads(raw_lock.decode("utf-8"))
        current_expiry = int(current["expires_at"])
    except Exception as exc:  # noqa: BLE001 — an unreadable lock must not wedge the job
        log.warning("existing lock unusable; treating as busy", extra={"key": lock_key})
        raise PipelineError("LOCK_CONFLICT", f"_LOCK is unusable ({type(exc).__name__})") from exc
    if current_expiry > now:
        return None
    # The ETag comes from the SAME GET whose body we inspected: a separate
    # HEAD could observe a newer lock acquired between the two requests.
    ok, new_etag = s3.renew_lock(lock_key, old_etag, _lock_body(instance_id_value, attempt, expires_at))
    if not ok:
        return None
    log.info("lock takeover", extra={"key": lock_key, "previous_expires_at": current_expiry})
    return LockState(key=lock_key, etag=new_etag, expires_at=expires_at)


def renew_lock(
    s3: S3IO,
    state: LockState,
    instance_id_value: str,
    attempt: int,
    ttl_sec: float,
    now: float,
) -> bool:
    """Renew our lock; ``False`` means we lost it (callers abort, transient).

    The conditional write on the stored ETag implements the "abort the job if
    the ETag changes" rule of CONTRACT.md section 2.
    """
    expires_at = int(now + ttl_sec)
    if state.etag is None:
        return False
    ok, new_etag = s3.renew_lock(state.key, state.etag, _lock_body(instance_id_value, attempt, expires_at))
    if not ok:
        return False
    state.etag = new_etag
    state.expires_at = expires_at
    return True


def _assert_disk_room(free_bytes: int, size_bytes: int, cfg: WorkerConfig) -> None:
    """Fail with ``DISK_FULL`` when the 2.5x + 20 GiB headroom is missing."""
    needed = int(cfg.disk.headroom_multiplier * max(size_bytes, 0) + cfg.disk.headroom_gib * _CHUNK)
    if free_bytes < needed:
        raise PipelineError(
            "DISK_FULL",
            f"free disk space {free_bytes} is below the required headroom "
            f"{needed} (2.5x source + {cfg.disk.headroom_gib} GiB)",
        )


def _urllib_poster(url: str, headers: Mapping[str, str], body: bytes, timeout_sec: float) -> "tuple[int, str]":
    """Production webhook poster: ONE POST, no retries (the emitter retries)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 — configured Railway endpoint
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310
            return int(response.status), str(getattr(response, "reason", "") or "")
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc.reason)


def _imds_getter(method: str, path: str, headers: Mapping[str, str]) -> "tuple[int, bytes]":
    """Production IMDSv2 client for the local instance facts."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 — link-local IMDS endpoint
        f"http://169.254.169.254{path}",
        data=(b"" if method == "PUT" else None),
        headers=dict(headers),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except OSError:
        return 0, b""


def _subprocess_runner(argv: Sequence[str]) -> Any:
    """Run one argv in its OWN process group and capture everything.

    ``start_new_session=True`` makes the child a process-group leader, so the
    deadline signaler can target the child's group without including the
    supervisor (the old ``os.killpg(os.getpgrp(), ...)`` killed us first).
    """
    import subprocess

    proc = subprocess.Popen(  # noqa: S603 — argv is built internally, never user data
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    supervisor_mod.CHILDREN.register(proc.pid)
    try:
        stdout, stderr = proc.communicate()
    finally:
        supervisor_mod.CHILDREN.unregister(proc.pid)
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def _static_monotonic() -> ClockFn:
    import time

    return time.monotonic


def _static_wall() -> ClockFn:
    import time

    return time.time


def _static_sleep(delay: float) -> None:
    import time

    time.sleep(delay)


def _build_executor() -> Executor:
    from concurrent.futures import ThreadPoolExecutor

    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="vg-webhook")


def default_collaborators(cfg: "WorkerConfig") -> Collaborators:
    """Build the REAL collaborators with boto3/urllib/subprocess.

    boto3 is imported HERE and now — never at module import time.
    """
    import boto3  # local import: keep module import offline-safe
    from botocore.config import Config

    region = cfg.aws_region
    client_cfg = Config(
        region_name=region,
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
    )
    return Collaborators(
        sqs=boto3.client("sqs", region_name=region, config=client_cfg),
        s3_factory=lambda: boto3.client("s3", region_name=region, config=client_cfg),
        secrets_client=boto3.client("secretsmanager", region_name=region, config=client_cfg),
        imds=_imds_getter,
        asg=boto3.client("autoscaling", region_name=region, config=client_cfg),
        container_runner=_subprocess_runner,
        ffmpeg_runner=_subprocess_runner,
        packager_runner=_subprocess_runner,
        disk_free=lambda path: shutil.disk_usage(path).free,
        monotonic=_static_monotonic(),
        wall_clock=_static_wall(),
        sleep=_static_sleep,
        signaler=ProcessGroupSignaler(),
        webhook_executor=_build_executor(),
    )


def _collect_versions(collab: Collaborators, cfg: WorkerConfig) -> "tuple[str, str]":
    """Best-effort ffmpeg/packager versions for manifest provenance."""
    ffmpeg_version = ""
    packager_version = ""
    try:
        result = collab.ffmpeg_runner([cfg.bins.ffmpeg, "-version"])
        first_line = (getattr(result, "stdout", "") or "").splitlines()
        ffmpeg_version = first_line[0].strip() if first_line else ""
    except Exception:  # noqa: BLE001 — provenance stays best-effort
        pass
    try:
        result = collab.packager_runner([cfg.bins.packager, "--version"])
        first_line = (getattr(result, "stdout", "") or "").splitlines()
        packager_version = first_line[0].strip() if first_line else ""
    except Exception:  # noqa: BLE001
        pass
    return ffmpeg_version, packager_version


def _now_rfc3339(wall_clock: ClockFn) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(wall_clock(), tz=UTC).isoformat()


def _dlq_envelope(
    original_body: str,
    failure_code: str,
    failure_detail: str,
    stage: str,
    job_id: str,
    run_epoch: "int | None",
    attempt: int,
) -> str:
    """DLQ message body: the original payload plus a machine-readable reason."""
    try:
        parsed: Any = json.loads(original_body)
    except json.JSONDecodeError:
        parsed = None
    original = parsed if isinstance(parsed, dict) else {"raw": original_body}
    return json.dumps(
        {
            "original": original,
            "error": {
                "code": failure_code,
                "stage": stage,
                "message": failure_detail,
                "job_id": job_id or None,
                "run_epoch": run_epoch,
                "attempt": attempt,
            },
        }
    )


@dataclass
class _FailRoute:
    """One failure's context: ``failed`` webhook + deterministic DLQ routing.

    The webhook fires when the failure is DETERMINISTIC (the DLQ send that
    follows leaves no later attempt to report it) or on the FINAL attempt;
    the DLQ send + message delete happen only for deterministic codes.
    """

    sqs: SqsClient
    cfg: WorkerConfig
    receipt: str
    raw_body: str
    attempt: int
    stage: str = "receive"
    emitter: "WebhookEmitter | None" = None
    job_id: str = ""
    run_epoch: "int | None" = None

    def dispatch(self, exc: BaseException, code: str) -> str:
        """Handle one failure; returns ``dlq`` or ``transient``."""
        deterministic = code in DETERMINISTIC_CODES
        final_attempt = self.attempt >= self.cfg.queue.max_receive_count
        log.error(
            "job failed",
            extra={
                "error_code": code,
                "stage": self.stage,
                "job_id": self.job_id,
                "deterministic": deterministic,
                "final_attempt": final_attempt,
            },
        )
        if self.emitter is not None and (deterministic or final_attempt):
            # ``failed`` is sent when the failure is DETERMINISTIC (the
            # message goes to the DLQ NOW, so no later attempt will ever
            # report it) or on the FINAL attempt (CONTRACT.md 3: a
            # transient failure must not permanently fail the video early).
            self.emitter.emit(
                "failed",
                qualifier=self.stage,
                payload={
                    "error_code": code,
                    "message": str(exc)[:500],
                    "stage_at_failure": self.stage,
                    "attempt": self.attempt,
                },
                retryable=False,
            )
            self.emitter.flush()
        if not deterministic:
            return "transient"  # leave the message; SQS returns it soon
        try:
            self.sqs.send_message(
                QueueUrl=self.cfg.queue.dlq_url,
                MessageBody=_dlq_envelope(
                    self.raw_body,
                    code,
                    str(exc)[:1000],
                    self.stage,
                    self.job_id,
                    self.run_epoch,
                    self.attempt,
                ),
            )
        except Exception as send_exc:  # noqa: BLE001 — DLQ outage must not mask the failure
            log.error("could not send to DLQ", extra={"error_code": code, "detail": type(send_exc).__name__})
        try:
            self.sqs.delete_message(QueueUrl=self.cfg.queue.jobs_url, ReceiptHandle=self.receipt)
        except Exception as del_exc:  # noqa: BLE001
            log.error("could not delete the failed message", extra={"detail": type(del_exc).__name__})
        return "dlq"


def _optional_emitter(
    parsed_body: object,
    cfg: WorkerConfig,
    collab: Collaborators,
    route: "_FailRoute",
) -> "WebhookEmitter | None":
    """Build an emitter from partially-valid message fields, else ``None``.

    Used when the message cannot be normalised but DOES look like a canonical
    JobMessage (so an orderable ``failed`` event exists). Any obstacle here
    degrades silently to ``None``: the DLQ still gets the message.
    """
    if not isinstance(parsed_body, dict):
        return None
    try:
        partial = JobMessage.model_validate(parsed_body)
    except Exception:  # noqa: BLE001 — no orderable run for this message
        return None
    try:
        response = collab.secrets_client.get_secret_value(SecretId=cfg.secrets.webhook_secret_arn)
        content = json.loads(str(response["SecretString"]))
        secret = str(content["secret"])
    except Exception:  # noqa: BLE001
        return None
    route.job_id = partial.job_id
    route.run_epoch = partial.run_epoch
    route.stage = "receive"
    return WebhookEmitter(
        job_id=partial.job_id,
        run_id=partial.run_id,
        run_epoch=partial.run_epoch,
        attempt=route.attempt,
        secret=secret,
        url=cfg.webhook.url,
        poster=collab.poster or _urllib_poster,
        clock=collab.wall_clock,
        sleep=collab.sleep,
        backoff=tuple(cfg.webhook.backoff),
        request_timeout_sec=cfg.webhook.request_timeout_sec,
        executor=None,  # synchronous: this path is short-lived and rare
    )


def _partial_job_id(parsed_body: object) -> str:
    """``job_id`` from a canonical message that failed validation, if present."""
    if isinstance(parsed_body, dict) and isinstance(parsed_body.get("job_id"), str):
        return str(parsed_body["job_id"])
    return ""


def _partial_run_epoch(parsed_body: object) -> "int | None":
    """``run_epoch`` from a canonical message that failed validation, if present."""
    if isinstance(parsed_body, dict) and isinstance(parsed_body.get("run_epoch"), int):
        return int(parsed_body["run_epoch"])
    return None


def _redelivery_done(
    s3: S3IO,
    done_key: str,
    job: JobMessage,
    attempt: int,
    emitter: WebhookEmitter,
    sqs: SqsClient,
    cfg: WorkerConfig,
    receipt: str,
) -> str:
    """A redelivery for a finished job: re-POST ``done`` (idempotent) + delete."""
    payload: dict = {}
    try:
        stored = json.loads(s3.get_object(done_key).decode("utf-8"))
        payload = dict(stored.get("done_payload") or {})
    except Exception:  # noqa: BLE001 — the marker alone proves completion
        log.warning("_DONE marker unreadable; deleting the redelivery anyway", extra={"key": done_key})
    emitter.emit("done", payload=payload, retryable=False)
    emitter.flush()
    try:
        sqs.delete_message(QueueUrl=cfg.queue.jobs_url, ReceiptHandle=receipt)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not delete the redelivery", extra={"detail": type(exc).__name__})
    log.info("redelivery idempotent", extra={"job_id": job.job_id, "attempt": attempt})
    return "idempotent"


def _terminate_best_effort(collab: Collaborators, cfg: WorkerConfig, instance_id_value: str) -> None:
    """Self-terminate through the ASG; failures degrade to shutdown fallback."""
    if not instance_id_value:
        return
    try:
        terminate_mod.terminate(collab.asg, instance_id_value, cfg.asg_name, _subprocess_runner)
    except Exception as exc:  # noqa: BLE001 — a job outcome must not be masked
        log.error("self termination failed", extra={"detail": type(exc).__name__})


def _terminate_with_imds(collab: Collaborators, cfg: WorkerConfig) -> None:
    """Best-effort terminate by resolving the instance id from IMDS right now.

    Used on every exit path where ``known_instance`` is still empty (no
    message at all, or the message failed before the identity was captured).
    An IMDS failure is swallowed: a job outcome must never be masked.
    """
    try:
        instance = terminate_mod.instance_id(collab.imds)
    except Exception:  # noqa: BLE001
        log.warning("cannot self-terminate: no IMDS instance id")
        return
    _terminate_best_effort(collab, cfg, instance)


def run(cfg: WorkerConfig, collab: Collaborators) -> str:
    """Process EXACTLY ONE SQS message and return an outcome identifier.

    Outcomes: ``done``, ``idempotent`` (finished redelivery), ``dlq``,
    ``transient``, ``busy``, ``no-message`` or ``test-event``. Exceptions
    that escape this function are start-up problems (config, logging), not
    job failures.
    """
    configure_logging(cfg.log_level)
    missing = cfg.missing_settings()
    if missing:
        raise ValueError(f"worker config is not ready; missing: {', '.join(missing)}")
    media: PipelineConfig = load_pipeline_config(cfg.pipeline_config_path)

    sqs = collab.sqs
    response = sqs.receive_message(
        QueueUrl=cfg.queue.jobs_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=cfg.queue.receive_wait_sec,
        AttributeNames=["ApproximateReceiveCount"],
    )
    messages = list(response.get("Messages") or [])
    if not messages:
        log.info("queue is empty; nothing to process")
        _terminate_with_imds(collab, cfg)
        return "no-message"
    message = messages[0]
    receipt = str(message.get("ReceiptHandle", ""))
    attributes = dict(message.get("Attributes") or {})
    try:
        attempt = int(attributes.get("ApproximateReceiveCount", "1"))
    except (TypeError, ValueError):
        attempt = 1
    raw_body = str(message.get("Body", ""))
    try:
        parsed_body: Any = json.loads(raw_body)
    except json.JSONDecodeError:
        parsed_body = None

    s3 = S3IO(collab.s3_factory, cfg.s3_bucket)

    def sidecar_loader(key: str) -> dict:
        """Read ``jobs/<job_id>/job.json``; ANY failure is a FileNotFoundError."""
        try:
            return sidecar_dict_from_bytes(s3.get_object(key))
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise maps this to SIDECAR_INVALID
            log.debug("sidecar read failed", extra={"key": key, "detail": type(exc).__name__})
            raise FileNotFoundError(key) from exc

    route = _FailRoute(sqs=sqs, cfg=cfg, receipt=receipt, raw_body=raw_body, attempt=attempt)
    # The instance id, once IMDS answers; the finally block terminates with
    # it, falling back to a fresh IMDS lookup when it was never captured.
    # Set via a one-element list so the closure below sees updates.
    known_instance: list[str] = []

    try:
        try:
            body_for_normalize: Any = parsed_body if isinstance(parsed_body, dict) else {"unparseable": raw_body[:200]}
            job: JobMessage = normalize_message(body_for_normalize, sidecar_loader)
        except TestEventIgnored:
            try:
                sqs.delete_message(QueueUrl=cfg.queue.jobs_url, ReceiptHandle=receipt)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not delete the test event", extra={"detail": type(exc).__name__})
            log.info("s3 test event ignored")
            return "test-event"
        except PipelineError as exc:
            route.job_id = _partial_job_id(parsed_body)
            route.run_epoch = _partial_run_epoch(parsed_body)
            route.emitter = _optional_emitter(parsed_body, cfg, collab, route)
            return route.dispatch(exc, exc.code)

        # Instance facts FIRST so every later path can self-terminate.
        try:
            instance = terminate_mod.instance_id(collab.imds)
        except RuntimeError as exc:
            # Without an instance id this box cannot ASG-terminate itself;
            # leave the message visible (infrastructure problem).
            log.error("cannot read the instance identity", extra={"detail": str(exc)})
            return route.dispatch(exc, INTERNAL_CODE)
        known_instance.append(instance)
        try:
            ami = terminate_mod.ami_id(collab.imds)
        except RuntimeError as exc:
            log.warning("cannot read the ami id", extra={"detail": str(exc)})
            ami = ""
        log.info(
            "job start",
            extra={
                "job_id": job.job_id,
                "run_id": job.run_id,
                "run_epoch": job.run_epoch,
                "attempt": attempt,
                "instance_id": instance,
                "ami_id": ami,
                "generator_image": cfg.images.generator,
            },
        )

        arns = {
            "webhook": cfg.secrets.webhook_secret_arn,
            "axinom": cfg.secrets.axinom_secret_arn,
            "watermark": cfg.secrets.watermark_secret_arn,
        }
        try:
            secrets = fetch_secrets(collab.secrets_client, arns)
        except ValueError as exc:
            # Secrets problems are infrastructure: leave the message visible.
            log.error("secret fetch failed", extra={"detail": str(exc)})
            return route.dispatch(exc, INTERNAL_CODE)

        emitter = WebhookEmitter(
            job_id=job.job_id,
            run_id=job.run_id,
            run_epoch=job.run_epoch,
            attempt=attempt,
            secret=dict(secrets["webhook"])["secret"],
            url=cfg.webhook.url,
            poster=collab.poster or _urllib_poster,
            clock=collab.wall_clock,
            sleep=collab.sleep,
            backoff=tuple(cfg.webhook.backoff),
            request_timeout_sec=cfg.webhook.request_timeout_sec,
            executor=collab.webhook_executor,
        )
        route.emitter = emitter
        route.job_id = job.job_id
        route.run_epoch = job.run_epoch
        return _process(
            cfg=cfg,
            collab=collab,
            media=media,
            s3=s3,
            sqs=sqs,
            job=job,
            emitter=emitter,
            route=route,
            instance=instance,
            ami=ami,
            watermark_secret=dict(secrets["watermark"])["secret"],
            watermark_version=secrets.version_of("watermark"),
            axinom=dict(secrets["axinom"]),
        )
    finally:
        if known_instance:
            _terminate_best_effort(collab, cfg, known_instance[0])
        else:
            _terminate_with_imds(collab, cfg)


def _process(
    *,
    cfg: WorkerConfig,
    collab: Collaborators,
    media: PipelineConfig,
    s3: S3IO,
    sqs: SqsClient,
    job: JobMessage,
    emitter: WebhookEmitter,
    route: _FailRoute,
    instance: str,
    ami: str,
    watermark_secret: str,
    watermark_version: str,
    axinom: dict,
) -> str:
    """The locked pipeline: supervisor, five stages, ``_DONE``, delete.

    Order per CONTRACT.md section 8: redelivery check -> disk gate -> lock ->
    supervisor (deadline + heartbeat) -> stages -> done -> _DONE -> delete.
    """
    ctx = stages.StageContext(
        job=job,
        media=media,
        aws=cfg,
        s3=s3,
        emitter=emitter,
        work=Path(cfg.work_dir),
        container_runner=collab.container_runner,
        ffmpeg_runner=collab.ffmpeg_runner,
        packager_runner=collab.packager_runner,
        axinom_post=collab.axinom_post,
        instance_id=instance,
        ami_id=ami,
        generator_image=cfg.images.generator,
    )
    ffmpeg_version, packager_version = _collect_versions(collab, cfg)
    ctx.packager_version = packager_version
    ctx.ffmpeg_version = ffmpeg_version
    axinom_secrets = AxinomSecrets.model_validate(
        {k: v for k, v in axinom.items() if k in AxinomSecrets.model_fields}
    )
    route.stage = ctx.stage

    final_outcome = "transient"
    lock_state: "LockState | None" = None
    lock_key = ctx.key("reports", _LOCK_SUFFIX)
    done_key = ctx.key("reports", _DONE_SUFFIX)
    try:
        marker: dict[str, Any] | None = None
        marker_unreadable = False
        try:
            marker = s3.get_json_or_none(done_key)
        except ValueError:
            marker_unreadable = True
            log.warning("_DONE marker unreadable", extra={"key": done_key})
        if marker is not None and int(marker.get("run_epoch", 0)) == job.run_epoch:
            # A redelivery for THIS run: idempotent done POST. An older marker
            # from a superseded run_epoch must not short-circuit a requeue.
            return _redelivery_done(s3, done_key, job, route.attempt, emitter, sqs, cfg, route.receipt)
        if marker_unreadable:
            # We cannot prove which run wrote a corrupt marker; keep the old
            # best-effort idempotency rather than risk a duplicate GPU run.
            return _redelivery_done(s3, done_key, job, route.attempt, emitter, sqs, cfg, route.receipt)
        _assert_disk_room(collab.disk_free(cfg.work_dir), job.size_bytes, cfg)

        now = collab.wall_clock()
        lock_state = acquire_lock(
            s3, lock_key, instance, route.attempt, cfg.deadlines.lock_ttl_sec, now
        )
        if lock_state is None:
            # Another live worker instance owns the lock. Do NOT delete the
            # message: a leftover lock from a failed attempt used to make this
            # path delete a job that nobody would ever finish. Leaving it
            # visible lets a later delivery (after expiry/release) process it.
            log.info("lock busy; leaving the message visible", extra={"job_id": job.job_id})
            return "busy"
    except PipelineError as exc:
        route.stage = ctx.stage
        return route.dispatch(exc, exc.code)
    except ContainerError as exc:
        route.stage = ctx.stage
        return route.dispatch(exc, CONTAINER_CODE)
    except DeadlineExceeded as exc:  # pragma: no cover — impossible before the supervisor
        return route.dispatch(exc, DEADLINE_CODE)
    except Exception as exc:  # noqa: BLE001 — last resort: transient, leave the message
        route.stage = ctx.stage
        return route.dispatch(exc, INTERNAL_CODE)

    def keepalive() -> None:
        """Every 300 s: refresh the visibility timeout, then renew the lock.

        Called by the supervisor heartbeat. An SQS hiccup is tolerated (the
        next beat recovers); a LOST LOCK aborts this attempt, so it raises.
        """
        try:
            sqs.change_message_visibility(
                QueueUrl=cfg.queue.jobs_url,
                ReceiptHandle=route.receipt,
                VisibilityTimeout=cfg.queue.visibility_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001 — one missed beat is recoverable
            log.warning("visibility heartbeat failed", extra={"detail": type(exc).__name__})
        renewed = renew_lock(
            s3,
            lock_state,
            instance,
            route.attempt,
            cfg.deadlines.lock_ttl_sec,
            collab.wall_clock(),
        )
        if not renewed:
            raise PipelineError("LOCK_CONFLICT", "lock ETag changed or the renewal failed")

    with Supervisor(
        cfg.deadlines.job_sec,
        heartbeat_sec=cfg.deadlines.heartbeat_sec,
        heartbeat=keepalive,
        clock=collab.monotonic,
        sleeper=collab.sleep,
        signaler=collab.signaler,
        kill_grace_sec=cfg.deadlines.kill_grace_sec,
    ) as supervisor:
        supervisor.start_background()
        try:
            probe = stages.validate.run_validate(ctx)
            supervisor.tick()
            with watermark_secret_file(watermark_secret, directory=Path(cfg.secret_file_dir)) as secret_path:
                stages.generate.run_generate(ctx, secret_path=secret_path)
            supervisor.tick()
            ladder_result = stages.ladder.run_ladder(ctx, probe)
            supervisor.tick()
            package_result = stages.package.run_package(ctx, probe, ladder_result.qualities, axinom_secrets)
            supervisor.tick()
            finalize_result = stages.finalize.run_finalize(
                ctx,
                probe,
                ladder_result.qualities,
                package_result,
                watermark_version=watermark_version,
                completed_at=_now_rfc3339(collab.wall_clock),
            )
            supervisor.tick()
            # The bounded ``done`` POST: emit, wait up to 5 s, THEN write
            # _DONE (CONTRACT.md section 3: _DONE is written after the
            # bounded done POST, not before).
            emitter.emit("done", payload=finalize_result.done_payload, retryable=False)
            emitter.flush()
            webhooks_ok = emitter.last_success is True
            s3.write_done(
                done_key,
                json.dumps(
                    {
                        "job_id": job.job_id,
                        "completed_at": _now_rfc3339(collab.wall_clock),
                        "manifest_key": finalize_result.manifest_key,
                        "webhooks_ok": webhooks_ok,
                        "attempt": route.attempt,
                        "run_epoch": job.run_epoch,
                        "done_payload": finalize_result.done_payload,
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
            sqs.delete_message(QueueUrl=cfg.queue.jobs_url, ReceiptHandle=route.receipt)
            log.info("job done", extra={"job_id": job.job_id, "webhooks_ok": webhooks_ok})
            final_outcome = "done"
        except PipelineError as exc:
            route.stage = ctx.stage
            final_outcome = route.dispatch(exc, exc.code)
        except DeadlineExceeded as exc:
            supervisor.enforce_deadline()
            route.stage = ctx.stage
            final_outcome = route.dispatch(exc, DEADLINE_CODE)
        except ContainerError as exc:
            route.stage = ctx.stage
            final_outcome = route.dispatch(exc, CONTAINER_CODE)
        except Exception as exc:  # noqa: BLE001 — last resort: transient, leave the message
            route.stage = ctx.stage
            final_outcome = route.dispatch(exc, INTERNAL_CODE)

    if final_outcome != "done" and lock_state is not None and lock_state.etag:
        # A failed attempt must not leave a live 1200 s lock behind: the SQS
        # message becomes visible after 900 s and the next worker would see a
        # "busy" lock. Expiring it (conditional on our ETag) lets the retry run.
        try:
            s3.expire_lock(lock_key, lock_state.etag)
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the outcome
            log.warning("could not expire the lock after failure", extra={"detail": type(exc).__name__})
    return final_outcome


def main(argv: "Sequence[str] | None" = None) -> int:
    """CLI entry: read env + config file, run one message, terminate.

    Used by ``python -m vg_worker.main`` (the systemd unit). The environment
    supplies every ``VG_*`` variable from CONTRACT.md section 5.
    """
    import argparse
    import os

    configure_logging(os.environ.get("VG_LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description="video-guard worker agent")
    parser.add_argument(
        "--config",
        default=os.environ.get("VG_WORKER_CONFIG", "/etc/vg/worker.yaml"),
        help="path to aws.worker.yaml (default: $VG_WORKER_CONFIG or /etc/vg/worker.yaml)",
    )
    args = parser.parse_args(argv)

    collab: "Collaborators | None" = None
    try:
        cfg = load_worker_config(args.config, dict(os.environ))
        collab = default_collaborators(cfg)
        outcome = run(cfg, collab)
        log.info("worker outcome", extra={"outcome": outcome})
        return 0
    except KeyboardInterrupt:
        raise
    except Exception:
        # run() converts job failures into outcomes; anything HERE is a
        # start-up/config problem. Exit non-zero so systemd reports it.
        log.exception("worker run failed at start-up")
        return 2
    finally:
        if collab is not None:
            collab.shutdown()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
