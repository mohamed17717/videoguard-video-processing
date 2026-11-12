"""Tests for ladder encode commands, audio extraction and thumbnails."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vg_pipeline.contracts import PipelineConfig, Rendition
from vg_pipeline.errors import PipelineError
from vg_pipeline.ladder import build_ffmpeg_command, cpu_crf, extract_audio, extract_thumbnails


def rendition_720() -> Rendition:
    return Rendition.model_validate(
        {
            "name": "720p",
            "width": 1280,
            "height": 720,
            "target_kbps": 2800,
            "maxrate_kbps": 3800,
            "bufsize_kbps": 7600,
            "nvenc_cq": 23,
        }
    )


class TestNvencCommand:
    def test_full_flag_set(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        cmd = build_ffmpeg_command(
            src=tmp_path / "source.mp4",
            out=tmp_path / "a_720p.mp4",
            r=rendition_720(),
            fps=29.97,
            cfg=pipeline_cfg,
            ffmpeg_bin="ffmpeg",
            has_audio=True,
            use_nvenc=True,
        )
        assert cmd[0] == "ffmpeg"
        assert cmd[-1] == str(tmp_path / "a_720p.mp4")
        assert "-i" in cmd and str(tmp_path / "source.mp4") in cmd
        # rate control: VBR + CQ, never CRF
        i = cmd.index("-rc")
        assert cmd[i + 1] == "vbr"
        i = cmd.index("-cq")
        assert cmd[i + 1] == "23"
        assert "-crf" not in cmd
        assert "libx264" not in cmd
        assert "h264_nvenc" in cmd
        i = cmd.index("-b:v")
        assert cmd[i + 1] == "2800k"
        i = cmd.index("-maxrate")
        assert cmd[i + 1] == "3800k"
        i = cmd.index("-bufsize")
        assert cmd[i + 1] == "7600k"
        # GOP: round(29.97 * 2) = 60, scene cut off, forced key frames every 2 s
        i = cmd.index("-g")
        assert cmd[i + 1] == "60"
        i = cmd.index("-keyint_min")
        assert cmd[i + 1] == "60"
        i = cmd.index("-sc_threshold")
        assert cmd[i + 1] == "0"
        i = cmd.index("-force_key_frames")
        assert cmd[i + 1] == "expr:gte(t,n_forced*2)"
        # CFR output
        i = cmd.index("-fps_mode")
        assert cmd[i + 1] == "cfr"
        i = cmd.index("-r")
        assert cmd[i + 1] == "29.97"
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
        assert "scale=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p" in cmd
        assert cmd[cmd.index("-movflags") + 1] == "+faststart"
        # audio re-encoded to stereo AAC 128k 48 kHz
        i = cmd.index("-c:a")
        assert cmd[i + 1] == "aac"
        assert cmd[cmd.index("-b:a") + 1] == "128k"
        assert cmd[cmd.index("-ar") + 1] == "48000"
        assert cmd[cmd.index("-ac") + 1] == "2"

    def test_integer_fps_formatting(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        cmd = build_ffmpeg_command(
            tmp_path / "s.mp4", tmp_path / "o.mp4", rendition_720(), 30.0, pipeline_cfg, "ffmpeg", False
        )
        assert cmd[cmd.index("-g") + 1] == "60"
        assert cmd[cmd.index("-r") + 1] == "30"

    def test_no_audio_flags_without_audio(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        cmd = build_ffmpeg_command(
            tmp_path / "s.mp4", tmp_path / "o.mp4", rendition_720(), 29.97, pipeline_cfg, "ffmpeg", False
        )
        assert "-c:a" not in cmd
        assert "-b:a" not in cmd


class TestCpuFallback:
    def test_libx264_with_crf_only(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        cmd = build_ffmpeg_command(
            tmp_path / "s.mp4",
            tmp_path / "o.mp4",
            rendition_720(),
            29.97,
            pipeline_cfg,
            "ffmpeg",
            True,
            use_nvenc=False,
        )
        assert "libx264" in cmd
        assert "h264_nvenc" not in cmd
        i = cmd.index("-crf")
        assert cmd[i + 1] == str(cpu_crf(rendition_720()))
        # NEVER both -crf and -cq; -rc is NVENC-only.
        assert "-cq" not in cmd
        assert "-rc" not in cmd
        # VBV caps still present
        assert cmd[cmd.index("-maxrate") + 1] == "3800k"
        assert cmd[cmd.index("-bufsize") + 1] == "7600k"

    def test_cpu_crf_mapping(self) -> None:
        assert cpu_crf(rendition_720()) == 21
        assert cpu_crf(Rendition.model_validate(rendition_720().model_dump() | {"nvenc_cq": 0})) == 0
        assert cpu_crf(Rendition.model_validate(rendition_720().model_dump() | {"nvenc_cq": 51})) == 49


class TestExtractAudio:
    def test_success_returns_out_path(self, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        out = extract_audio(tmp_path / "s.mp4", tmp_path / "audio.m4a", "ffmpeg", run=run)
        assert out == tmp_path / "audio.m4a"
        cmd = seen[0]
        assert cmd[0] == "ffmpeg"
        assert cmd[-1] == str(tmp_path / "audio.m4a")
        assert "-vn" in cmd
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-b:a") + 1] == "128k"
        assert cmd[cmd.index("-ar") + 1] == "48000"
        assert cmd[cmd.index("-ac") + 1] == "2"

    def test_no_audio_stream_returns_none(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Output file #0 does not contain any stream\n"
            )

        assert extract_audio(tmp_path / "s.mp4", tmp_path / "a.m4a", "ffmpeg", run=run) is None

    def test_real_failure_raises(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Permission denied")

        with pytest.raises(PipelineError) as excinfo:
            extract_audio(tmp_path / "s.mp4", tmp_path / "a.m4a", "ffmpeg", run=run)
        assert excinfo.value.code == "ENCODER_UNAVAILABLE"

    def test_missing_binary_raises(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            raise FileNotFoundError("ffmpeg not found")

        with pytest.raises(PipelineError) as excinfo:
            extract_audio(tmp_path / "s.mp4", tmp_path / "a.m4a", "ffmpeg", run=run)
        assert excinfo.value.code == "ENCODER_UNAVAILABLE"


class TestExtractThumbnails:
    def test_six_thumbnails(self, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        thumbs = extract_thumbnails(
            tmp_path / "variant_a.mp4", tmp_path / "thumbs", 6, 100.0, "ffmpeg", run=run
        )
        assert [p.name for p in thumbs] == [f"thumb_{i:02d}.jpg" for i in range(1, 7)]
        assert len(seen) == 6
        stamps = [float(cmd[cmd.index("-ss") + 1]) for cmd in seen]
        # timestamps spread over the duration, none at 0 or at the end
        assert stamps[0] > 0
        assert stamps[-1] < 100.0
        assert stamps == sorted(stamps)
        assert all(cmd[cmd.index("-frames:v") + 1] == "1" for cmd in seen)
        assert all(cmd[-1].endswith(".jpg") for cmd in seen)

    def test_zero_count_runs_nothing(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            raise AssertionError("ffmpeg must not run for count=0")

        assert extract_thumbnails(tmp_path / "s.mp4", tmp_path, 0, 100.0, "ffmpeg", run=run) == []

    def test_failure_raises(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with pytest.raises(PipelineError) as excinfo:
            extract_thumbnails(tmp_path / "s.mp4", tmp_path, 6, 100.0, "ffmpeg", run=run)
        assert excinfo.value.code == "ENCODER_UNAVAILABLE"

    def test_bad_duration_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineError) as excinfo:
            extract_thumbnails(tmp_path / "s.mp4", tmp_path, 6, 0.0, "ffmpeg", run=lambda *a, **k: None)
        assert excinfo.value.code == "INPUT_CONTRACT"


def test_extract_thumbnails_creates_missing_directory(tmp_path: Path) -> None:
    out_dir = tmp_path / "thumbs"
    assert not out_dir.exists()

    def run(cmd, **kwargs):  # noqa: ANN001, ARG001
        Path(cmd[-1]).write_bytes(b"jpg")
        return subprocess.CompletedProcess(cmd, 0)

    thumbs = extract_thumbnails(tmp_path / "src.mp4", out_dir, 2, 10.0, "ffmpeg", run=run)
    assert out_dir.is_dir()
    assert len(thumbs) == 2
