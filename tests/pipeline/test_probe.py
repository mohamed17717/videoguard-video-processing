"""Tests for ffprobe parsing and the full input-contract validation."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable

import pytest

from vg_pipeline.contracts import PipelineConfig, ProbeResult
from vg_pipeline.errors import PipelineError
from vg_pipeline.probe import expected_qualities, probe, validate_input

RunLike = Callable[..., subprocess.CompletedProcess]


def ffprobe_payload(**overrides: Any) -> dict:
    """A compliant 1280x720 29.97 CFR H.264/AAC MP4 as ffprobe would report it."""
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1280,
        "height": 720,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "30000/1001",
        "avg_frame_rate": "30000/1001",
        "nb_frames": "8973",
        "bits_per_raw_sample": "8",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "color_range": "tv",
        "duration": "299.200000",
    }
    audio = {
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
        "duration": "299.200000",
    }
    streams = overrides.pop("streams", [video, audio])
    fmt = overrides.pop("format", {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "299.200000"})
    return {"streams": streams, "format": fmt}


def fake_runner(payload: dict, returncode: int = 0, stderr: str = "") -> RunLike:
    def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        assert "ffprobe" in cmd[0] or cmd[0].endswith("ffprobe")
        assert "-show_streams" in cmd and "-show_format" in cmd and "-print_format" in cmd
        assert "json" in cmd
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=json.dumps(payload), stderr=stderr
        )

    return run


def compliant_probe_result(**overrides: Any) -> ProbeResult:
    base = dict(
        codec="h264",
        width=1280,
        height=720,
        pix_fmt="yuv420p",
        fps=29.97,
        duration_sec=600.0,
        frame_count=17982,
        bit_depth=8,
        color_transfer="bt709",
        color_primaries="bt709",
        color_range="tv",
        has_audio=True,
        audio_codec="aac",
        streams=2,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        vfr=False,
        audio_streams=1,
    )
    return ProbeResult.model_validate(base | overrides)


class TestProbe:
    def test_parses_fake_ffprobe_json(self, pipeline_cfg: PipelineConfig) -> None:
        result = probe("/work/input/source.mp4", pipeline_cfg, "ffprobe", run=fake_runner(ffprobe_payload()))
        assert result.codec == "h264"
        assert (result.width, result.height) == (1280, 720)
        assert result.pix_fmt == "yuv420p"
        # 30000/1001 exactly, NOT the old max(parseFPS, 30) rounding.
        assert abs(result.fps - 29.97002997002997) < 1e-9
        assert result.frame_count == 8973
        assert result.bit_depth == 8
        assert result.color_transfer == "bt709"
        assert result.color_range == "tv"
        assert result.has_audio is True
        assert result.audio_codec == "aac"
        assert result.streams == 2
        assert result.container == "mov,mp4,m4a,3gp,3g2,mj2"
        assert result.vfr is False
        assert result.audio_streams == 1

    def test_vfr_detected_from_rate_mismatch(self, pipeline_cfg: PipelineConfig) -> None:
        payload = ffprobe_payload()
        payload["streams"][0]["avg_frame_rate"] = "29970/1001"
        result = probe("/x.mp4", pipeline_cfg, "ffprobe", run=fake_runner(payload))
        assert result.vfr is True

    def test_frame_count_falls_back_to_duration_times_fps(self, pipeline_cfg: PipelineConfig) -> None:
        payload = ffprobe_payload()
        del payload["streams"][0]["nb_frames"]
        result = probe("/x.mp4", pipeline_cfg, "ffprobe", run=fake_runner(payload))
        assert result.frame_count == int(round(299.2 * (30000 / 1001)))

    def test_hevc_reported(self, pipeline_cfg: PipelineConfig) -> None:
        payload = ffprobe_payload()
        payload["streams"][0]["codec_name"] = "hevc"
        result = probe("/x.mp4", pipeline_cfg, "ffprobe", run=fake_runner(payload))
        assert result.codec == "hevc"

    def test_ffprobe_failure_raises_input_contract(self, pipeline_cfg: PipelineConfig) -> None:
        runner = fake_runner({}, returncode=1, stderr="Invalid data found")
        with pytest.raises(PipelineError) as excinfo:
            probe("/x.mp4", pipeline_cfg, "ffprobe", run=runner)
        assert excinfo.value.code == "INPUT_CONTRACT"

    def test_no_video_stream_raises(self, pipeline_cfg: PipelineConfig) -> None:
        payload = {"streams": [ffprobe_payload()["streams"][1]], "format": {"format_name": "mp4"}}
        with pytest.raises(PipelineError) as excinfo:
            probe("/x.mp4", pipeline_cfg, "ffprobe", run=fake_runner(payload))
        assert excinfo.value.code == "INPUT_CONTRACT"

    def test_bad_json_raises(self, pipeline_cfg: PipelineConfig) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

        with pytest.raises(PipelineError) as excinfo:
            probe("/x.mp4", pipeline_cfg, "ffprobe", run=run)
        assert excinfo.value.code == "INPUT_CONTRACT"

    def test_missing_binary_raises(self, pipeline_cfg: PipelineConfig) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            raise FileNotFoundError("no ffprobe here")

        with pytest.raises(PipelineError) as excinfo:
            probe("/x.mp4", pipeline_cfg, "ffprobe", run=run)
        assert excinfo.value.code == "INPUT_CONTRACT"


class TestValidateInput:
    def test_compliant_input_passes(self, pipeline_cfg: PipelineConfig) -> None:
        validate_input(compliant_probe_result(), pipeline_cfg, 1073741824)

    def test_untagged_colour_passes_with_warning(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(color_transfer=None, color_primaries=None, color_range=None)
        validate_input(result, pipeline_cfg, 1024)

    def test_vfr_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(vfr=True), pipeline_cfg, 1024)
        assert excinfo.value.code == "INPUT_CONTRACT"
        assert "constant frame rate" in excinfo.value.message

    def test_ten_bit_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(
                compliant_probe_result(pix_fmt="yuv420p10le", bit_depth=10), pipeline_cfg, 1024
            )
        assert "bit depth" in excinfo.value.message

    def test_hdr_transfer_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(color_transfer="smpte2084"), pipeline_cfg, 1024)
        assert "HDR" in excinfo.value.message

    def test_hlg_transfer_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(color_transfer="arib-std-b67"), pipeline_cfg, 1024)
        assert excinfo.value.code == "INPUT_CONTRACT"

    def test_hdr_primaries_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(color_primaries="bt2020"), pipeline_cfg, 1024)
        assert "HDR" in excinfo.value.message

    def test_wrong_colour_transfer_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(color_transfer="iec61966-2-1"), pipeline_cfg, 1024)
        assert "color_transfer" in excinfo.value.message

    def test_full_range_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(color_range="pc"), pipeline_cfg, 1024)
        assert "color_range" in excinfo.value.message

    def test_odd_dimensions_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(width=853), pipeline_cfg, 1024)
        assert "not even" in excinfo.value.message

    def test_wrong_pix_fmt_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(pix_fmt="yuv422p"), pipeline_cfg, 1024)
        assert "yuv422p" in excinfo.value.message

    def test_non_aac_audio_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(audio_codec="ac3"), pipeline_cfg, 1024)
        assert "ac3" in excinfo.value.message

    def test_two_audio_streams_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(audio_streams=2, streams=3), pipeline_cfg, 1024)
        assert "audio streams 2 exceed" in excinfo.value.message

    def test_wrong_video_codec_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(codec="vp9"), pipeline_cfg, 1024)
        assert "vp9" in excinfo.value.message

    def test_wrong_container_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(container="matroska,webm"), pipeline_cfg, 1024)
        assert "matroska" in excinfo.value.message

    def test_all_problems_reported_together(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(vfr=True, pix_fmt="yuv422p", width=853, height=481)
        with pytest.raises(PipelineError) as excinfo:
            validate_input(result, pipeline_cfg, 1024)
        message = excinfo.value.message
        assert "constant frame rate" in message
        assert "yuv422p" in message
        assert "not even" in message


class TestCaps:
    def test_oversize_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(), pipeline_cfg, 2147483649)
        assert "max_bytes" in excinfo.value.message

    def test_too_long_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(duration_sec=3601.0), pipeline_cfg, 1024)
        assert "max_duration_sec" in excinfo.value.message

    def test_too_wide_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(width=3842), pipeline_cfg, 1024)
        assert "max_width" in excinfo.value.message

    def test_too_tall_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(height=2162), pipeline_cfg, 1024)
        assert "max_height" in excinfo.value.message

    def test_too_many_frames_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(frame_count=216001), pipeline_cfg, 1024)
        assert "max_frames" in excinfo.value.message

    def test_too_many_streams_rejected(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(PipelineError) as excinfo:
            validate_input(compliant_probe_result(streams=9), pipeline_cfg, 1024)
        assert "max_streams" in excinfo.value.message

    def test_caps_boundary_values_pass(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(
            duration_sec=3600.0,
            width=3840,
            height=2160,
            frame_count=216000,
            streams=8,
        )
        validate_input(result, pipeline_cfg, 2147483648)


class TestExpectedQualities:
    def test_all_qualities_fit_source(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(width=1920, height=1080)
        assert [r.name for r in expected_qualities(result, pipeline_cfg)] == [
            "720p",
            "480p",
            "360p",
            "240p",
        ]

    def test_skips_upscaling(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(width=854, height=480)
        assert [r.name for r in expected_qualities(result, pipeline_cfg)] == ["480p", "360p", "240p"]

    def test_skips_all_for_tiny_source(self, pipeline_cfg: PipelineConfig) -> None:
        result = compliant_probe_result(width=320, height=180)
        assert expected_qualities(result, pipeline_cfg) == []

    def test_portrait_source_never_upscaled(self, pipeline_cfg: PipelineConfig) -> None:
        # 640x720 source: 720p and 480p would upscale the width, so they go.
        result = compliant_probe_result(width=640, height=720)
        assert [r.name for r in expected_qualities(result, pipeline_cfg)] == ["360p", "240p"]
