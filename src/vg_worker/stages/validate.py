"""``validated`` stage: download, probe, hard caps, ClamAV.

The original is downloaded to a read-only ``/work/input``, then ffprobe and
ClamAV run INSIDE the network-less validator container (CONTRACT.md section
8, step 2). The host re-checks the container's probe report against the full
input contract of ``configs/pipeline.yaml`` and FAILS CLOSED:
``INPUT_CONTRACT`` for probe/contract problems, ``CLAMAV`` for malware and
for a broken scanner. Nothing is converted (Option A: no normalisation).
"""

from __future__ import annotations

from vg_pipeline.contracts import ProbeResult
from vg_pipeline.errors import PipelineError
from vg_pipeline.logging import get_logger
from vg_pipeline.probe import _parse_probe_json, validate_input

from vg_worker import container as container_mod
from vg_worker.stages import StageContext, atomic_write, probe_summary

log = get_logger("vg_worker.stages.validate")


def run_validate(ctx: StageContext) -> ProbeResult:
    """Validate the upload and emit ``validated``; returns the probe report."""
    ctx.stage = "validated"
    input_dir = ctx.work / "input"
    source = input_dir / "source.mp4"
    try:
        payload = ctx.s3.get_object(ctx.incoming_key())
    except Exception as exc:  # noqa: BLE001 — decide by status: 404 is fatal
        from vg_worker.s3io import _http_status

        if _http_status(exc) == 404:
            raise PipelineError("INPUT_CONTRACT", f"cannot read {ctx.incoming_key()} (NoSuchKey)") from exc
        raise  # everything else is infra/transient: leave the message
    if not payload:
        raise PipelineError("INPUT_CONTRACT", "the uploaded source.mp4 is empty")
    atomic_write(source, payload, mode=0o444)
    log.info("source downloaded", extra={"bytes": len(payload)})

    verdict = container_mod.run_validator(
        image=ctx.aws.images.validator,
        input_mount=input_dir,
        ffprobe_bin=ctx.aws.bins.ffprobe,
        clamscan_bin="clamscan",
        runner=ctx.container_runner,  # type: ignore[arg-type]
    )

    clam_exit = verdict.clamscan_exit
    if clam_exit == 1:
        raise PipelineError("CLAMAV", "malware found in the upload")
    if clam_exit != 0:
        raise PipelineError("CLAMAV", f"clamscan failed with exit {clam_exit}: {verdict.clam_output[-200:]}")

    probe_doc = verdict.ffprobe
    if verdict.ffprobe_exit != 0 or probe_doc is None:
        raise PipelineError(
            "INPUT_CONTRACT",
            f"ffprobe failed inside the validator (exit {verdict.ffprobe_exit})",
        )
    try:
        probe = _parse_probe_json(_json_text(probe_doc))
    except PipelineError:
        raise
    validate_input(probe, ctx.media, len(payload))

    ctx.emitter.emit(
        "validated",
        payload={"probe": probe_summary(probe), "clamav": "clean", "caps": "pass"},
    )
    return probe


def _json_text(document: dict) -> str:
    """Serialise the container's probe report back to ffprobe-shaped text."""
    import json

    return json.dumps(document)
