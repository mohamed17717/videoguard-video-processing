"""Docker invocation for the validator and generator containers.

The worker agent runs on the host and drives two hardened containers
(CONTRACT.md sections 5, 8). Both use ``--network=none``, ``--read-only``,
``--cap-drop ALL``, ``--security-opt no-new-privileges`` and run as the
non-root UID/GID 10001. The secret never crosses the container boundary
except through the read-only tmpfs mount.

All argv strings live here verbatim so tests can assert on them without
running docker. The ``runner`` is injectable: production passes a
subprocess-based runner, tests pass a fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, Union

from vg_pipeline.errors import PipelineError
from vg_pipeline.logging import get_logger

log = get_logger("vg_worker.container")

RunnerLike = Callable[[Sequence[str]], Any]
PathLike = Union[str, Path]

_CONTAINER_UID = "10001:10001"
_TMPFS_FLAGS = "/tmp:rw,noexec,nosuid,size=512m"
# The generator REJECTS an existing --output-dir, so the mount is the
# writable PARENT and the requested directory is a fresh child.
_GENERATOR_OUTPUT_CHILD = "generated"

# The generator's completion contract: exactly these files must exist and
# be non-empty when the container exits 0 (CONTRACT.md section 8, step 3).
GENERATOR_OUTPUT_FILES: tuple[str, ...] = (
    "variant_a.mp4",
    "variant_b.mp4",
    "config.json",
    "generation_report.json",
)

# Exit codes the generator container yields when the worker interrupts it.
SIGINT_EXIT = 130
SIGTERM_EXIT = 143


class Runner(Protocol):
    """Run one argv and return a CompletedProcess-like result."""

    def __call__(self, argv: Sequence[str]) -> Any: ...


class ContainerError(Exception):
    """The container could not run or exited in an unexpected way.

    Treated as TRANSIENT by the worker: hardware, driver, image and daemon
    faults can clear on the next attempt. Interrupt exits (130/143) are also
    surfaced as this error so the SQS message stays visible.
    """

    def __init__(self, image: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.image = image
        self.exit_code = exit_code
        self.message = message

    def __str__(self) -> str:
        return f"[container {self.image} exit {self.exit_code}] {self.message}"


def _security_flags() -> list[str]:
    return [
        "--network=none",  # no network at runtime, ever
        "--read-only",  # immutable root filesystem
        "--cap-drop", "ALL",  # no Linux capabilities
        "--security-opt", "no-new-privileges",  # no setuid escalation
        "--user", _CONTAINER_UID,
    ]


def build_generator_argv(
    image: str,
    input_mount: PathLike,
    output_mount: PathLike,
    secret_mount: PathLike,
    backend: str = "cuda",
    *,
    docker_bin: str = "docker",
) -> list[str]:
    """The exact ``docker run`` argv for the watermark generator container.

    Structure (CONTRACT.md section 8): GPUs, no network, read-only rootfs,
    dropped capabilities, no-new-privileges, non-root user, the input and the
    secret mounted read-only, an explicitly writable per-job PARENT mount
    (needed because ``--read-only`` makes the image's ``/work/output`` part
    of the read-only rootfs); the generator is asked for a NEW child
    directory because production rejects an existing ``--output-dir``, a
    small noexec tmpfs for ``/tmp``, the secret path
    passed as an env var (NOT the value), and the NVIDIA driver capabilities
    the container may use.
    """
    if not image:
        raise ValueError("generator image reference is empty")
    return [
        docker_bin,
        "run",
        "--rm",
        "--gpus", "all",
        *_security_flags(),
        "-v", f"{input_mount}:/work/input:ro",
        "-v", f"{output_mount}:/work/output:rw",
        "-v", f"{secret_mount}:/run/secrets/watermark:ro",
        "--tmpfs", _TMPFS_FLAGS,
        "-e", "WATERMARK_SECRET_FILE=/run/secrets/watermark",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility",
        image,
        "--input", "/work/input/source.mp4",
        "--output-dir", f"/work/output/{_GENERATOR_OUTPUT_CHILD}",
        "--backend", backend,
    ]


def build_validator_argv(
    image: str,
    input_mount: PathLike,
    ffprobe_bin: str,
    clamscan_bin: str,
    *,
    docker_bin: str = "docker",
) -> list[str]:
    """The exact ``docker run`` argv for the validation container.

    Same hardening as the generator, minus the GPU (both probes are CPU
    work). The binary paths are passed so the image can use exactly what the
    worker config names; the image bakes the same pinned ffprobe by default.
    """
    if not image:
        raise ValueError("validator image reference is empty")
    return [
        docker_bin,
        "run",
        "--rm",
        *_security_flags(),
        "-v", f"{input_mount}:/work/input:ro",
        "--tmpfs", _TMPFS_FLAGS,
        image,
        "--input", "/work/input/source.mp4",
        "--ffprobe-bin", ffprobe_bin,
        "--clamscan-bin", clamscan_bin,
    ]


def _run(image: str, runner: Runner, argv: Sequence[str]) -> Any:
    """Invoke the runner; wrap transport failures into ContainerError."""
    try:
        return runner(argv)
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 — runner transport failures
        raise ContainerError(image, -1, f"cannot run docker: {type(exc).__name__}: {exc}") from exc


def _parse_exit(image: str, result: Any) -> int:
    code = getattr(result, "returncode", None)
    if code is None:
        raise ContainerError(image, -1, "runner result carries no returncode")
    return int(code)


def _stderr_tail(result: Any, limit: int = 500) -> str:
    stderr = (getattr(result, "stderr", "") or "").strip()
    return stderr[-limit:]


def _parse_json_document(text: str, what: str) -> dict[str, Any] | None:
    """Parse the last JSON object in ``text`` (the completion/verdict JSON).

    Programs log human-readable lines before the final JSON, so whole-text
    parsing fails; scanning line by line from the bottom finds the object.
    """
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    log.error(f"container produced no {what} JSON")
    return None


def _assert_outputs(output_mount: PathLike) -> dict[str, Path]:
    """Verify the four atomic generator outputs exist and are non-empty."""
    root = Path(output_mount)
    files: dict[str, Path] = {}
    for name in GENERATOR_OUTPUT_FILES:
        path = root / name
        try:
            if not path.is_file() or path.stat().st_size == 0:
                raise PipelineError("GENERATOR_EXIT_2", f"generator did not produce {name}")
        except OSError as exc:
            raise PipelineError("GENERATOR_EXIT_2", f"cannot stat {name}: {exc}") from exc
        files[name] = path
    return files


class GeneratorResult:
    """Products of one successful generator container run."""

    def __init__(self, completion: dict[str, Any], files: dict[str, Path]) -> None:
        self.completion: dict[str, Any] = completion
        self.files: dict[str, Path] = files

    @property
    def variant_a(self) -> Path:
        return self.files["variant_a.mp4"]

    @property
    def variant_b(self) -> Path:
        return self.files["variant_b.mp4"]

    @property
    def config_json(self) -> Path:
        return self.files["config.json"]

    @property
    def generation_report(self) -> Path:
        return self.files["generation_report.json"]


def run_generator(
    image: str,
    input_mount: PathLike,
    output_mount: PathLike,
    secret_mount: PathLike,
    backend: str,
    runner: Runner,
    *,
    docker_bin: str = "docker",
) -> GeneratorResult:
    """Run the generator container and verify its four atomic outputs.

    Exit-code contract:

    * 0 with a completion JSON and 4 non-empty files -> success;
    * exit 2 -> the SOURCE generator rejected the environment/input
      (:class:`PipelineError` ``GENERATOR_EXIT_2``, deterministic -> DLQ);
    * exit 130/143 (interrupt) -> :class:`ContainerError` propagates
      (transient; the SQS message stays visible);
    * any other exit -> :class:`ContainerError` (transient).
    """
    log.info("generator container start", extra={"image": image, "backend": backend})
    argv = build_generator_argv(image, input_mount, output_mount, secret_mount, backend, docker_bin=docker_bin)
    result = _run(image, runner, argv)
    code = _parse_exit(image, result)
    stderr_tail = _stderr_tail(result)
    if code == 2:
        raise PipelineError("GENERATOR_EXIT_2", f"generator rejected the job: {stderr_tail}")
    if code in (SIGINT_EXIT, SIGTERM_EXIT):
        raise ContainerError(image, code, f"generator was interrupted: {stderr_tail}")
    if code != 0:
        raise ContainerError(image, code, f"generator exited {code}: {stderr_tail}")
    stdout = getattr(result, "stdout", "") or ""
    completion = _parse_json_document(stdout, "completion")
    if completion is None:
        # The runtime contract is: print one completion JSON to stdout. The
        # real muxer behaviour is a deferred gate; a container without the
        # completion JSON is treated as a generator contract breach.
        raise PipelineError("GENERATOR_EXIT_2", "generator stdout had no completion JSON")
    status = str(completion.get("status", "")).lower()
    if status and status not in ("ok", "complete", "success"):
        raise PipelineError("GENERATOR_EXIT_2", f"generator completion status {status!r}")
    files = _assert_outputs(Path(output_mount) / _GENERATOR_OUTPUT_CHILD)
    log.info("generator container done", extra={"image": image, "files": sorted(files)})
    return GeneratorResult(completion=completion, files=files)


class ValidatorVerdict:
    """Raw verdict document of the validator container, parsed."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document: dict[str, Any] = document

    @property
    def ffprobe(self) -> "dict[str, Any] | None":
        value = self.document.get("ffprobe")
        return value if isinstance(value, dict) else None

    @property
    def ffprobe_exit(self) -> int:
        return int(self.document.get("ffprobe_exit", -1))

    @property
    def clamscan_exit(self) -> int:
        return int(self.document.get("clamscan_exit", -1))

    @property
    def clam_output(self) -> str:
        return str(self.document.get("clam_output", ""))


def run_validator(
    image: str,
    input_mount: PathLike,
    ffprobe_bin: str,
    clamscan_bin: str,
    runner: Runner,
    *,
    docker_bin: str = "docker",
) -> ValidatorVerdict:
    """Run the validation container and return its parsed verdict document.

    The container script exits 0 and encodes BOTH probes in one JSON object:

    ``{"ffprobe": <raw ffprobe json or null>, "ffprobe_exit": n,
    "clamscan_exit": n, "clam_output": "...", "error": "..."}``

    The worker maps business meaning elsewhere (see the validate stage);
    here only the container transport contract is enforced: docker-level
    failures raise :class:`ContainerError` (transient).
    """
    log.info("validator container start", extra={"image": image})
    argv = build_validator_argv(image, input_mount, ffprobe_bin, clamscan_bin, docker_bin=docker_bin)
    result = _run(image, runner, argv)
    code = _parse_exit(image, result)
    if code in (SIGINT_EXIT, SIGTERM_EXIT):
        raise ContainerError(image, code, "validator was interrupted")
    if code != 0:
        raise ContainerError(image, code, f"validator exited {code}: {_stderr_tail(result)}")
    stdout = getattr(result, "stdout", "") or ""
    document = _parse_json_document(stdout, "verdict")
    if document is None:
        raise ContainerError(image, 0, "validator stdout had no verdict JSON")
    log.info("validator container done", extra={"image": image, "keys": sorted(document)})
    return ValidatorVerdict(document)
