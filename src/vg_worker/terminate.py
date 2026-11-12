"""Self-termination: IMDSv2 identity + ASG terminate (CONTRACT.md section 8).

Every finished attempt ends with the instance terminating ITSELF:

1. the instance id comes from IMDSv2 (session-token PUT, then GET) — there
   are NO environment-variable instance facts;
2. ``autoscaling:TerminateInstanceInAutoScalingGroup`` with
   ``ShouldDecrementDesiredCapacity=True`` — this instance is the ONLY
   scale-in path of the scale-out-only ASG, so a replacement is never
   launched;
3. three retries on ``ScalingActivityInProgress`` / ``ValidationError``
   (the ASG can be mid-rescale), then the raw ``shutdown -h now`` fallback
   (the launch template already says "terminate" on shutdown).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from vg_pipeline.logging import get_logger

log = get_logger("vg_worker.terminate")

ImdsGetter = Callable[[str, str, Mapping[str, str]], "tuple[int, bytes]"]
ShutdownRunner = Callable[[Sequence[str]], Any]
SleepFn = Callable[[float], None]

# IMDSv2 session token lives long enough for the whole job.
_TOKEN_PATH = "/latest/api/token"
_TOKEN_TTL_HEADER = "21600"
_TOKEN_HEADER = "X-aws-ec2-metadata-token"
_TOKEN_TTL_KEY = "X-aws-ec2-metadata-token-ttl-seconds"

# ASG errors worth one quick retry (CONTRACT.md section 8, step 7).
_RETRYABLE_ERROR_CODES = frozenset({"ScalingActivityInProgress", "ValidationError"})
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SEC = 5.0

# The agent runs as a non-root user, so the fallback uses passwordless sudo
# (granted by install.sh). "now" = immediate halt; the launch template turns
# InstanceInitiatedShutdownBehavior into termination.
_SHUTDOWN_FALLBACK: tuple[str, ...] = ("sudo", "-n", "shutdown", "-h", "now")


class AsgClient(Protocol):
    """The subset of a boto3 autoscaling client the worker needs."""

    def terminate_instance_in_auto_scaling_group(self, **kwargs: Any) -> Mapping[str, Any]: ...


def instance_id(imds_getter: ImdsGetter) -> str:
    """This instance's id via IMDSv2: first PUT a session token, then GET.

    Raises ``RuntimeError`` when IMDS denies either call (unexpected status
    or empty body).
    """
    status, body = imds_getter("PUT", _TOKEN_PATH, {_TOKEN_TTL_KEY: _TOKEN_TTL_HEADER})
    if status != 200:
        raise RuntimeError(f"IMDSv2 token request failed with status {status}")
    token = body.decode("utf-8", errors="replace").strip()
    if not token:
        raise RuntimeError("IMDSv2 returned an empty session token")
    status, body = imds_getter("GET", "/latest/meta-data/instance-id", {_TOKEN_HEADER: token})
    if status != 200:
        raise RuntimeError(f"IMDSv2 instance-id request failed with status {status}")
    value = body.decode("utf-8", errors="replace").strip()
    if not value:
        raise RuntimeError("IMDSv2 returned an empty instance id")
    return value


def ami_id(imds_getter: ImdsGetter) -> str:
    """This instance's AMI id via IMDSv2 (for the manifest + logs)."""
    status, body = imds_getter("PUT", _TOKEN_PATH, {_TOKEN_TTL_KEY: _TOKEN_TTL_HEADER})
    if status != 200:
        raise RuntimeError(f"IMDSv2 token request failed with status {status}")
    token = body.decode("utf-8", errors="replace").strip()
    status, body = imds_getter("GET", "/latest/meta-data/ami-id", {_TOKEN_HEADER: token})
    if status != 200:
        raise RuntimeError(f"IMDSv2 ami-id request failed with status {status}")
    return body.decode("utf-8", errors="replace").strip()


def _error_code(exc: Exception) -> str | None:
    """The botocore-style error code of an API exception, if present."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def terminate(
    asg_client: AsgClient,
    instance_id_value: str,
    asg_name: str,
    runner: ShutdownRunner,
    *,
    sleeper: SleepFn = time.sleep,
    retry_delay_sec: float = _RETRY_DELAY_SEC,
) -> bool:
    """Terminate this instance through the ASG; return True when API-issued.

    Exactly ``ShouldDecrementDesiredCapacity=True`` is used — NEVER
    ``--no-should-decrement-desired-capacity`` (that would launch a
    replacement). Retry up to 3 times on the two retryable codes, then fall
    back to ``shutdown -h now`` (launch template -> terminate) and return
    False so the caller knows it used the fallback.
    """
    if not instance_id_value:
        raise RuntimeError("refusing to terminate without an instance id")
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            asg_client.terminate_instance_in_auto_scaling_group(
                InstanceId=instance_id_value,
                ShouldDecrementDesiredCapacity=True,
            )
            log.info("instance terminated via ASG", extra={"asg_name": asg_name, "attempt": attempt})
            return True
        except Exception as exc:  # noqa: BLE001 — need the code to decide the retry
            last_error = exc
            code = _error_code(exc)
            if code in _RETRYABLE_ERROR_CODES and attempt < _MAX_ATTEMPTS:
                log.warning(
                    "ASG termination retryable",
                    extra={"asg_name": asg_name, "attempt": attempt, "error_code": code},
                )
                sleeper(retry_delay_sec)
                continue
            break
    log.error(
        "ASG termination failed; falling back to shutdown",
        extra={"asg_name": asg_name, "error_code": _error_code(last_error) if last_error else None},
    )
    try:
        runner(_SHUTDOWN_FALLBACK)
    except Exception as exc:  # noqa: BLE001 — nothing left to do but report
        log.error("shutdown fallback failed", extra={"detail": type(exc).__name__})
    return False
