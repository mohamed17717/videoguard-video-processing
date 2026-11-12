"""``masters_ready`` stage: run the generator container and publish artifacts.

The generator runs ONCE on the fully hardened GPU container (CONTRACT.md
section 8, step 3): ``--gpus all``, no network, read-only rootfs, dropped
capabilities, no-new-privileges, non-root UID 10001, the input and the
watermark secret read-only, one writable per-job output mount and a small
noexec tmpfs.

On success the worker has already verified the four atomic outputs (the
container module does that), computes the source SHA-256, publishes
``masters/<job>/variant_{a,b}.mp4`` (storage class from ``retention``) and
the two report files under ``reports/<job>/`` (STANDARD), then emits
``masters_ready``.
"""

from __future__ import annotations

import os
from pathlib import Path

from vg_pipeline.logging import get_logger

from vg_worker import container as container_mod
from vg_worker.s3io import make_tags
from vg_worker.stages import StageContext, streaming_sha256

log = get_logger("vg_worker.stages.generate")


def _prepare_output_dir(path: Path) -> None:
    """Create the writable per-job output mount.

    Because the container runs as UID/GID 10001, the directory must be owned
    by 10001 when the worker runs as root (the AMI service case). Otherwise
    the invoking host user is already the owner and nothing is changed.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(path, 10001, 10001)


class GeneratorArtifacts:
    """What ``generate`` produced, for reports in the provenance manifest."""

    def __init__(self, masters: dict[str, str], config_key: str, generation_report_key: str, completion: dict) -> None:
        self.master_keys: dict[str, str] = masters
        self.config_key: str = config_key
        self.generation_report_key: str = generation_report_key
        self.completion: dict = completion


def run_generate(ctx: StageContext, secret_path: Path) -> GeneratorArtifacts:
    """Run the generator and publish masters + reports; emit ``masters_ready``.

    ``secret_path`` is the tmpfs watermark-secret file staged by ``main``.
    It is passed ONLY into the container argv; it never appears in a log
    line, an S3 object or a report.
    """
    ctx.stage = "masters_ready"
    if ctx.masters:
        raise RuntimeError("run_generate must run exactly once per job")
    out_dir = ctx.work / "generator-output"
    _prepare_output_dir(out_dir)

    ctx.source_sha256 = streaming_sha256(ctx.work / "input" / "source.mp4")
    result = container_mod.run_generator(
        image=ctx.generator_image or ctx.aws.images.generator,
        input_mount=ctx.work / "input",
        output_mount=out_dir,
        secret_mount=secret_path,
        backend=ctx.aws.backend,
        runner=ctx.container_runner,  # type: ignore[arg-type]
    )
    ctx.completion = result.completion
    ctx.masters = {"a": result.variant_a, "b": result.variant_b}

    retention = ctx.job.retention
    master_keys: dict[str, str] = {}
    for name, path in result.files.items():
        if not name.endswith(".mp4"):
            continue  # config.json / generation_report.json go to reports/
        key = ctx.key("masters", name)
        ctx.s3.put_file(
            key,
            path,
            storage_class=ctx.class_of("master"),
            tags=make_tags(retention),
        )
        master_keys[name.replace(".mp4", "")] = key

    report_keys: dict[str, str] = {}
    for name in ("config.json", "generation_report.json"):
        key = ctx.key("reports", name)
        ctx.s3.put_file(
            key,
            result.files[name],
            storage_class=ctx.class_of("standard"),
            tags=make_tags(retention),
        )
        report_keys[name] = key

    ctx.emitter.emit(
        "masters_ready",
        payload={"master_keys": master_keys, "report_keys": report_keys, "sha256": ctx.source_sha256},
    )
    log.info("masters published", extra={"masters": sorted(master_keys.values())})
    return GeneratorArtifacts(
        masters=dict(master_keys),
        config_key=report_keys["config.json"],
        generation_report_key=report_keys["generation_report.json"],
        completion=dict(result.completion),
    )
