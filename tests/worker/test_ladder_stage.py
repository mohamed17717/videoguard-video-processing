"""Ladder stage: NVENC/CPU rate control, fallback, audio, thumbnails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vg_pipeline.contracts import JobMessage, load_pipeline_config
from vg_pipeline.errors import PipelineError
from vg_pipeline.probe import _parse_probe_json
from vg_worker.stages import StageContext
from vg_worker.stages.ladder import detect_nvenc, run_ladder

from conftest import FakeS3, PIPELINE_CONFIG_PATH, job_model
from conftest import PROBE_DOCUMENT


@pytest.fixture
def probe() -> object:
    return _parse_probe_json(json.dumps(PROBE_DOCUMENT))


class SpyEmitter:
    """An emitter that records instead of POSTing (stage-check fixture)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def emit(self, stage: str, qualifier: str = "", payload: dict | None = None, retryable: bool = False) -> None:
        self.calls.append((stage, qualifier, dict(payload or {})))

    def flush(self, timeout: float = 5.0) -> None:
        return None


@pytest.fixture
def ladder_env(tmp_path: Path, world):
    """Masters on disk + a wired StageContext, monoidally safe per test."""
    work = tmp_path / "work"
    world.cfg.work_dir = str(work)
    (work / "input").mkdir(parents=True)
    (work / "input" / "source.mp4").write_bytes(b"src" * 4)
    out = work / "generator-output"
    out.mkdir(parents=True)
    (out / "variant_a.mp4").write_bytes(b"A" * 2048)
    (out / "variant_b.mp4").write_bytes(b"B" * 2048)

    job: JobMessage = job_model()
    ctx = StageContext(
        job=job,
        media=load_pipeline_config(PIPELINE_CONFIG_PATH),
        aws=world.cfg,
        s3=FakeS3(),
        emitter=SpyEmitter(),
        work=work,
        ffmpeg_runner=world.ffmpeg_runner(),
        packager_runner=world.packager_runner(),
        generator_image=world.cfg.images.generator,
    )
    ctx.masters = {"a": out / "variant_a.mp4", "b": out / "variant_b.mp4"}
    return ctx, world


def test_all_four_rungs_encoded_for_both_variants(ladder_env, probe) -> None:
    ctx, world = ladder_env
    result = run_ladder(ctx, probe)
    assert [r.name for r in result.qualities] == ["720p", "480p", "360p", "240p"]
    encodes = [c for c in world.ffmpeg_calls if c[-1].endswith(".mp4")]
    # 2 variants x 4 qualities = 8 encode runs (one command each)
    assert len(encodes) == 8
    for index, cmd in enumerate(encodes):
        cmd_tail = cmd[-1]
        variant = "a" if f"{ctx.work}/encoded/a/" in cmd_tail else "b"
        expected_quality = ["720p", "480p", "360p", "240p"][index % 4]
        assert cmd_tail == str(ctx.work / "encoded" / variant / f"{expected_quality}.mp4")


def test_nvenc_rate_control_rules(ladder_env, probe) -> None:
    from vg_pipeline.contracts import load_pipeline_config

    ladder = {r.name: r for r in load_pipeline_config(PIPELINE_CONFIG_PATH).ladder}
    ctx, world = ladder_env
    run_ladder(ctx, probe)
    for cmd in (c for c in world.ffmpeg_calls if c[-1].endswith(".mp4")):
        quality = Path(cmd[-1]).stem
        rendition = ladder[quality]
        joined = " ".join(cmd)
        assert "h264_nvenc" in joined
        assert "libx264" not in joined
        assert cmd[cmd.index("-rc") + 1] == "vbr"
        assert int(cmd[cmd.index("-cq") + 1]) == rendition.nvenc_cq
        assert "-crf" not in cmd  # NEVER both -crf and -cq (CONTRACT.md 4)
        assert cmd[cmd.index("-fps_mode") + 1] == "cfr"
        assert cmd[cmd.index("-r") + 1] == "30"
        assert cmd[cmd.index("-g") + 1] == "60"  # round(fps * gop_seconds)
        assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"


def test_audio_extracted_once_and_six_thumbnails(ladder_env, probe) -> None:
    ctx, world = ladder_env
    result = run_ladder(ctx, probe)
    audio_cmds = [c for c in world.ffmpeg_calls if c[-1].endswith(".m4a")]
    assert len(audio_cmds) == 1, "the audio tree is extracted exactly ONCE"
    assert audio_cmds[0][audio_cmds[0].index("-vn") - 1] == "-i" or "-vn" in audio_cmds[0]
    assert result.audio_path.is_file() and result.audio_path.name == "audio.m4a"
    assert len(result.thumbs) == 6
    thumb_cmds = [c for c in world.ffmpeg_calls if c[-1].endswith(".jpg")]
    assert len(thumb_cmds) == 6
    names = sorted(p.name for p in result.thumbs)
    assert names == [f"thumb_{n:02d}.jpg" for n in range(1, 7)]


def test_cpu_path_when_nvenc_absent(ladder_env, probe) -> None:
    ctx, world = ladder_env
    world.nvenc_present = False
    run_ladder(ctx, probe)
    for cmd in (c for c in world.ffmpeg_calls if c[-1].endswith(".mp4")):
        joined = " ".join(cmd)
        assert "libx264" in joined and "h264_nvenc" not in joined
        assert "-crf" in cmd and "-cq" not in cmd


def test_nvenc_failure_retries_with_libx264_from_same_binary(ladder_env, probe) -> None:
    ctx, world = ladder_env
    # detect still sees nvenc; every NVENC run fails but the retry succeeds.
    world.ffmpeg_fail_when = "h264_nvenc"
    result = run_ladder(ctx, probe)
    assert len(result.qualities) == 4
    encode_cmds = [c for c in world.ffmpeg_calls if c[-1].endswith(".mp4")]
    failed = [c for c in encode_cmds if "h264_nvenc" in " ".join(c)]
    retried = [c for c in encode_cmds if "libx264" in " ".join(c)]
    assert len(failed) == 8  # every first attempt failed on the GPU path
    assert len(retried) == 8  # every retry produced output via the CPU path


def test_both_paths_dead_fails_closure(ladder_env, probe) -> None:
    ctx, world = ladder_env
    world.ffmpeg_fail_when = "-y -i"  # nvenc AND the libx264 retry both fail
    with pytest.raises(PipelineError) as excinfo:
        run_ladder(ctx, probe)
    assert excinfo.value.code == "ENCODER_UNAVAILABLE"


def test_missing_master_fails(ladder_env, probe) -> None:
    ctx, world = ladder_env
    ctx.masters = {}
    with pytest.raises(PipelineError) as excinfo:
        run_ladder(ctx, probe)
    assert excinfo.value.code == "ENCODER_UNAVAILABLE"


def test_source_too_small_for_any_rung(ladder_env) -> None:
    ctx, world = ladder_env
    tiny = _parse_probe_json(json.dumps({
        "streams": [dict(PROBE_DOCUMENT["streams"][0], width=200, height=100)],
        "format": PROBE_DOCUMENT["format"],
    }))
    with pytest.raises(PipelineError) as excinfo:
        run_ladder(ctx, tiny)
    assert excinfo.value.code == "INPUT_CONTRACT"


def test_detect_nvenc_missing_binary_is_false(ladder_env, probe) -> None:
    ctx, world = ladder_env
    assert detect_nvenc(ctx.aws.bins.ffmpeg, world.ffmpeg_runner()) is True

    def broken(argv: list[str]) -> object:
        raise OSError("ffmpeg not found")

    assert detect_nvenc("missing", broken) is False


def test_worker_config_work_dir_is_used(ladder_env, probe) -> None:
    """The cfg work_dir (not a hardcoded path) receives all local artifacts."""
    ctx, world = ladder_env
    run_ladder(ctx, probe)
    assert (Path(world.cfg.work_dir) / "audio.m4a").is_file()
    assert (Path(world.cfg.work_dir) / "thumbs").is_dir()
