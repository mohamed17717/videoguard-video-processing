"""IMDSv2 identity and the ASG self-termination with shutdown fallback."""

from __future__ import annotations

import pytest

from vg_worker.terminate import instance_id, ami_id, terminate

from conftest import FakeAsg, FakeAwsClientError, SleepRecorder


def test_instance_id_uses_token_then_get() -> None:
    calls: list[tuple[str, str]] = []

    def imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        calls.append((method, path))
        if headers.get("X-aws-ec2-metadata-token-ttl-seconds") is not None:
            return 200, b"session-token"
        if headers.get("X-aws-ec2-metadata-token") == "session-token":
            return 200, b"i-0deadbeef012345678"
        return 403, b""

    assert instance_id(imds) == "i-0deadbeef012345678"
    assert calls == [("PUT", "/latest/api/token"), ("GET", "/latest/meta-data/instance-id")]


def test_token_failure_raises() -> None:
    def imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        return 403, b"metadata disabled"

    with pytest.raises(RuntimeError):
        instance_id(imds)


def test_empty_instance_body_raises() -> None:
    def imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        return 200, b"   "

    with pytest.raises(RuntimeError):
        instance_id(imds)


def test_ami_id_flow() -> None:
    def imds(method: str, path: str, headers: dict) -> tuple[int, bytes]:
        return 200, b"ami-0123456789deadbef"

    assert ami_id(imds) == "ami-0123456789deadbef"


def test_terminate_decrements_desired_capacity() -> None:
    asg = FakeAsg()
    runner = SleepRecorder()
    ok = terminate(asg, "i-0abc", "vg-workers-dev", runner)
    assert ok is True
    (call,) = asg.calls
    assert call["InstanceId"] == "i-0abc"
    # THE critical flag: a replacement instance must never be launched.
    assert call["ShouldDecrementDesiredCapacity"] is True


def test_terminate_retries_scaling_in_progress() -> None:
    asg = FakeAsg(failures=[("ScalingActivityInProgress", 2)])
    sleep = SleepRecorder()
    ok = terminate(asg, "i-0abc", "vg-workers-dev", SleepRecorder(), sleeper=sleep)
    assert ok is True
    assert len(asg.calls) == 3  # two retryable failures + one success


def test_terminate_retries_validation_error() -> None:
    asg = FakeAsg(failures=[("ValidationError", 1)])
    sleep = SleepRecorder()
    ok = terminate(asg, "i-0abc", "vg-workers-dev", SleepRecorder(), sleeper=sleep)
    assert ok is True
    assert len(asg.calls) == 2


@pytest.mark.parametrize("code", ["ScalingActivityInProgress", "ValidationError"])
def test_terminate_falls_back_to_shutdown_after_three_failures(code: str) -> None:
    asg = FakeAsg(failures=[(code, 3)])
    sleep = SleepRecorder()
    shutdown_calls: list[list[str]] = []

    def runner(argv: list[str]) -> None:
        shutdown_calls.append(list(argv))

    ok = terminate(asg, "i-0abc", "vg-workers-dev", runner, sleeper=sleep)
    assert ok is False  # the fallback ran instead of the ASG call
    assert len(asg.calls) == 3
    assert shutdown_calls == [["sudo", "-n", "shutdown", "-h", "now"]]
    assert len(sleep.sleeps) == 2  # between the three attempts


def test_non_retryable_error_goes_straight_to_shutdown() -> None:
    asg = FakeAsg(failures=[("AccessDenied", 1)])
    shutdown_calls: list[list[str]] = []

    def runner(argv: list[str]) -> None:
        shutdown_calls.append(list(argv))

    ok = terminate(asg, "i-0abc", "vg-workers-dev", runner)
    assert ok is False
    assert len(asg.calls) == 1
    assert shutdown_calls == [["sudo", "-n", "shutdown", "-h", "now"]]


def test_terminate_refuses_without_instance_id() -> None:
    with pytest.raises(RuntimeError, match="instance id"):
        terminate(FakeAsg(), "", "vg-workers-dev", SleepRecorder())


def test_shutdown_fallback_failure_is_survivable() -> None:
    asg = FakeAsg(failures=[("ScalingActivityInProgress", 3)])

    def failing_runner(argv: list[str]) -> None:
        raise OSError("halting failed")

    ok = terminate(asg, "i-0abc", "vg-workers-dev", failing_runner, sleeper=SleepRecorder())
    assert ok is False  # even with the fallback broken, terminate() must not raise


def test_aws_client_error_shape_is_detected() -> None:
    error = FakeAwsClientError("ScalingActivityInProgress")
    assert error.response["Error"]["Code"] == "ScalingActivityInProgress"
