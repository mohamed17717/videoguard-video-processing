"""Tests for the pydantic contracts and config loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vg_pipeline.contracts import (
    AxinomSecrets,
    CencKeys,
    JobMessage,
    JobSidecar,
    ManifestSet,
    PipelineConfig,
    ProbeResult,
    Rendition,
    ScanResult,
    StreamSpec,
    WebhookEvent,
    load_pipeline_config,
)
from vg_pipeline.errors import PipelineError


def sidecar_data() -> dict:
    return {
        "job_id": "0f0e0d0c-1111-4222-8333-444455556666",
        "run_id": "aaaa0b0c-1111-4222-8333-444455556666",
        "run_epoch": 1,
        "video_public_id": "77665544-1111-4222-8333-444455556666",
        "tenant_slug": "acme",
        "content_id": "77665544-1111-4222-8333-444455556666",
        "size_bytes": 1073741824,
        "trace_id": "deadbeef-1111-4222-8333-444455556666",
        "created_at": "2026-01-01T00:00:00Z",
    }


class TestJobSidecar:
    def test_defaults_match_contract(self) -> None:
        sidecar = JobSidecar.model_validate(sidecar_data())
        assert sidecar.schema_version == 1
        assert sidecar.content_type == "video/mp4"
        assert sidecar.retention == "archive"
        assert sidecar.reason == "s3"

    def test_missing_required_field_rejected(self) -> None:
        data = sidecar_data()
        del data["job_id"]
        with pytest.raises(ValidationError):
            JobSidecar.model_validate(data)

    def test_bad_retention_rejected(self) -> None:
        data = sidecar_data() | {"retention": "ttl90"}
        with pytest.raises(ValidationError):
            JobSidecar.model_validate(data)

    def test_run_epoch_must_be_positive(self) -> None:
        data = sidecar_data() | {"run_epoch": 0}
        with pytest.raises(ValidationError):
            JobSidecar.model_validate(data)


class TestJobMessage:
    def test_reason_literal_accepted(self) -> None:
        message = JobMessage.model_validate(sidecar_data() | {"reason": "requeue"})
        assert message.reason == "requeue"

    def test_bad_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobMessage.model_validate(sidecar_data() | {"reason": "duplicate"})

    def test_message_is_a_sidecar(self) -> None:
        message = JobMessage.model_validate(sidecar_data())
        assert isinstance(message, JobSidecar)


class TestWebhookEvent:
    def test_defaults(self) -> None:
        event = WebhookEvent.model_validate(
            {
                "job_id": "job",
                "run_id": "run",
                "run_epoch": 2,
                "stage": "validated",
                "seq": 1,
                "attempt": 1,
                "timestamp": 1730000000,
            }
        )
        assert event.qualifier == ""
        assert event.retryable is False
        assert event.payload == {}
        assert event.schema_version == 1

    def test_seq_and_attempt_must_be_positive(self) -> None:
        data = {
            "job_id": "job",
            "run_id": "run",
            "run_epoch": 2,
            "stage": "validated",
            "seq": 0,
            "attempt": 1,
            "timestamp": 1730000000,
        }
        with pytest.raises(ValidationError):
            WebhookEvent.model_validate(data)


class TestSmallModels:
    def test_rendition_accepts_ladder_values(self) -> None:
        rendition = Rendition.model_validate(
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
        assert rendition.name == "720p"

    def test_rendition_rejects_bad_cq(self) -> None:
        with pytest.raises(ValidationError):
            Rendition.model_validate(
                {
                    "name": "720p",
                    "width": 1280,
                    "height": 720,
                    "target_kbps": 2800,
                    "maxrate_kbps": 3800,
                    "bufsize_kbps": 7600,
                    "nvenc_cq": 99,
                }
            )

    def test_axinom_secrets_default_scheme(self) -> None:
        secrets = AxinomSecrets.model_validate(
            {
                "signing_key_hex": "00" * 32,
                "signing_iv_hex": "00" * 16,
                "endpoint": "https://cenc.example.invalid",
                "provider_name": "vg",
            }
        )
        assert secrets.scheme == "cbcs"

    def test_scan_result_and_cenc_keys(self) -> None:
        assert ScanResult(clean=True, output="").clean is True
        keys = CencKeys(
            key_id_hex="aa" * 16,
            key_hex="bb" * 16,
            iv_hex="cc" * 16,
            pssh_hex="00",
            skd_uri="skd://vg/x",
        )
        assert keys.key_hex == "bb" * 16

    def test_manifest_set_holds_bytes(self) -> None:
        manifest_set = ManifestSet(files={"renditions/j/hls/master.m3u8": b"#EXTM3U\n"})
        assert manifest_set.files["renditions/j/hls/master.m3u8"] == b"#EXTM3U\n"

    def test_stream_spec_paths(self, tmp_path: Path) -> None:
        spec = StreamSpec(
            input=tmp_path / "a" / "720p.mp4",
            stream="video",
            init_segment=tmp_path / "a" / "720p" / "init.mp4",
            segment_template=tmp_path / "a" / "720p" / "seg_$Number$.m4s",
            playlist_name="media_720p.m3u8",
            variant="a",
            quality="720p",
        )
        assert spec.stream == "video"
        assert spec.quality == "720p"

    def test_stream_spec_rejects_unknown_stream(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            StreamSpec(
                input=tmp_path / "x.mp4",
                stream="data",
                init_segment=tmp_path / "init.mp4",
                segment_template=tmp_path / "seg_$Number$.m4s",
                playlist_name="x.m3u8",
                variant="a",
                quality="720p",
            )


class TestPipelineConfig:
    def test_load_shipped_yaml_yields_four_renditions(self, pipeline_cfg: PipelineConfig) -> None:
        assert pipeline_cfg.schema_version == 1
        assert pipeline_cfg.profile == "hevc-v1"
        assert pipeline_cfg.segment_duration_sec == 2
        assert pipeline_cfg.gop_seconds == 2
        assert pipeline_cfg.thumbnail_count == 6
        assert pipeline_cfg.drm["scheme"] == "cbcs"
        assert pipeline_cfg.drm["systems"] == ["Widevine", "PlayReady", "FairPlay"]
        assert [r.name for r in pipeline_cfg.ladder] == ["720p", "480p", "360p", "240p"]
        assert pipeline_cfg.ladder[0].width == 1280
        assert pipeline_cfg.ladder[0].target_kbps == 2800
        assert pipeline_cfg.ladder[0].maxrate_kbps == 3800
        assert pipeline_cfg.ladder[0].bufsize_kbps == 7600
        assert pipeline_cfg.ladder[0].nvenc_cq == 23
        assert pipeline_cfg.ladder[3].width == 426
        assert pipeline_cfg.ladder[3].nvenc_cq == 25
        contract = pipeline_cfg.input_contract
        assert contract["require_cfr"] is True
        assert contract["reject_hdr"] is True
        assert contract["max_bit_depth"] == 8
        assert contract["allowed_pix_fmts"] == ["yuv420p", "nv12"]
        assert contract["allowed_audio_codecs"] == ["aac"]
        assert contract["max_audio_streams"] == 1
        assert contract["allowed_containers"] == ["mov", "mp4", "m4a", "3gp", "3g2", "mj2"]
        assert pipeline_cfg.caps["max_bytes"] == 2147483648
        assert pipeline_cfg.caps["max_duration_sec"] == 3600
        assert pipeline_cfg.caps["max_frames"] == 216000

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_pipeline_config(tmp_path / "nope.yaml")

    def test_non_mapping_yaml_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_pipeline_config(path)

    def test_wrong_schema_version_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("schema_version: 9\nprofile: x\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_pipeline_config(path)


class TestProbeResult:
    def test_build_minimal(self) -> None:
        result = ProbeResult(
            codec="h264",
            width=1280,
            height=720,
            pix_fmt="yuv420p",
            fps=29.97,
            duration_sec=100.0,
            frame_count=2997,
            bit_depth=8,
            has_audio=True,
            audio_codec="aac",
            streams=2,
            container="mov,mp4,m4a,3gp,3g2,mj2",
            vfr=False,
        )
        assert result.audio_streams is None
        assert result.color_transfer is None


def test_pipeline_error_carries_code_and_message() -> None:
    error = PipelineError("INPUT_CONTRACT", "bad input")
    assert error.code == "INPUT_CONTRACT"
    assert error.message == "bad input"
    assert "[INPUT_CONTRACT]" in str(error)
    from vg_pipeline.errors import ERROR_CODES

    assert error.code in ERROR_CODES
