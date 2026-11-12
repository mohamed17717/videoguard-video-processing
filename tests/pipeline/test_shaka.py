"""Tests for the Shaka packager command and the aligned-timeline gate."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from vg_pipeline.contracts import CencKeys, PipelineConfig, StreamSpec
from vg_pipeline.errors import PipelineError
from vg_pipeline.mp4 import canonicalize_init
from vg_pipeline.shaka import build_packager_args, check_aligned

QUALITIES = ("720p", "480p", "360p", "240p")

KID = "aa" * 16
KEY = "bb" * 16
IV = "cc" * 16
PSSH = "00" * 8
SKD = "skd://vg-test-provider/11111111-2222-3333-4444-555555555555"


def keys() -> CencKeys:
    return CencKeys(key_id_hex=KID, key_hex=KEY, iv_hex=IV, pssh_hex=PSSH, skd_uri=SKD)


def build_streams(root: Path) -> list[StreamSpec]:
    """The 9 descriptors of one packaging run: 8 video + 1 audio."""
    streams: list[StreamSpec] = []
    for variant in ("a", "b"):
        for quality in QUALITIES:
            base = root / variant / quality
            streams.append(
                StreamSpec(
                    input=root / f"{variant}_{quality}.mp4",
                    stream="video",
                    init_segment=base / "init.mp4",
                    segment_template=base / "seg_$Number$.m4s",
                    playlist_name=f"media_{quality}.m3u8",
                    variant=variant,
                    quality=quality,
                )
            )
    streams.append(
        StreamSpec(
            input=root / "audio.m4a",
            stream="audio",
            init_segment=root / "audio" / "init.mp4",
            segment_template=root / "audio" / "seg_$Number$.m4s",
            playlist_name="audio.m3u8",
            variant="shared",
            quality="audio",
        )
    )
    return streams


def full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    body = bytes([version]) + flags.to_bytes(3, "big") + payload
    # box total = 4 (size) + 4 (type) + len(body); the size field counts all of it
    return struct.pack(">I", 8 + len(body)) + box_type + body


def container(box_type: bytes, *children: bytes) -> bytes:
    payload = b"".join(children)
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def init_bytes(creation: int = 1) -> bytes:
    """Minimal moov{mvhd} with a nonzero creation_time (muxer wall-clock)."""
    mvhd = full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", creation, creation, 1000, 2000),
    )
    return container(b"moov", mvhd)


def segment_bytes(base_time: int) -> bytes:
    tfdt = full_box(b"tfdt", 0, 0, struct.pack(">I", base_time))
    traf = container(b"traf", tfdt)
    moof = container(b"moof", traf)
    styp = container(b"styp", b"msdh")
    return styp + moof


def write_quality_tree(root: Path, quality: str, count: int = 2) -> None:
    """Write a/<quality> and b/<quality> trees.

    The inits differ ONLY by the muxer wall-clock creation_time; segments are
    equal with increasing decode times.
    """
    a_init = init_bytes(creation=1)
    b_init = init_bytes(creation=1_700_000_000)
    (root / "a" / quality).mkdir(parents=True, exist_ok=True)
    (root / "b" / quality).mkdir(parents=True, exist_ok=True)
    (root / "a" / quality / "init.mp4").write_bytes(a_init)
    (root / "b" / quality / "init.mp4").write_bytes(b_init)
    for index in range(1, count + 1):
        stamp = (index - 1) * 2000
        (root / "a" / quality / f"seg_{index:05d}.m4s").write_bytes(segment_bytes(stamp))
        (root / "b" / quality / f"seg_{index:05d}.m4s").write_bytes(segment_bytes(stamp))


class TestBuildPackagerArgs:
    def test_nine_descriptors_and_flags(
        self, pipeline_cfg: PipelineConfig, tmp_path: Path
    ) -> None:
        root = tmp_path / "out"
        streams = build_streams(root)
        args = build_packager_args(streams, keys(), pipeline_cfg, root, "packager")

        assert args[0] == "packager"
        descriptors = [a for a in args if "stream=" in a]
        assert len(descriptors) == 9
        video = [a for a in descriptors if "stream=video" in a]
        audio = [a for a in descriptors if "stream=audio" in a]
        assert len(video) == 8
        assert len(audio) == 1
        # order: a/720p, b/720p, a/480p, b/480p, ... then audio
        expected_pairs = [(v, q) for v in ("a", "b") for q in QUALITIES]
        for descriptor, (variant, quality) in zip(video, expected_pairs):
            assert "stream=video" in descriptor
            assert f"init_segment={root / variant / quality / 'init.mp4'}" in descriptor
            assert f"segment_template={root / variant / quality / 'seg_$Number$.m4s'}" in descriptor
            assert f"playlist_name=media_{quality}.m3u8" in descriptor
            assert descriptor.startswith(f"input={root / f'{variant}_{quality}.mp4'},")
        assert "stream=audio" in audio[0]
        assert "playlist_name=audio.m3u8" in audio[0]

    def test_drm_flags_single_run(
        self, pipeline_cfg: PipelineConfig, tmp_path: Path
    ) -> None:
        args = build_packager_args(build_streams(tmp_path), keys(), pipeline_cfg, tmp_path, "packager")
        joined = args
        assert joined.count(f"--keys=key_id={KID}:key={KEY}") == 1
        assert f"--iv={IV}" in joined
        assert f"--pssh={PSSH}" in joined
        assert f"--hls_key_uri={SKD}" in joined
        assert "--enable_raw_key_encryption" in joined
        assert "--protection_scheme=cbcs" in joined
        assert "--protection_systems=Widevine,PlayReady,FairPlay" in joined
        assert "--clear_lead=0" in joined
        i = joined.index("--segment_duration")
        assert joined[i + 1] == "2"
        assert "--generate_static_live_mpd" in joined
        # generated manifests go to temp paths and are discarded
        i = joined.index("--hls_master_playlist_output")
        assert joined[i + 1].endswith("_shaka_master.m3u8")
        i = joined.index("--mpd_output")
        assert joined[i + 1].endswith("_shaka_stream.mpd")
        # one run means exactly one --keys flag; no repeated encryption flags
        assert sum(1 for a in joined if a.startswith("--keys=")) == 1
        assert sum(1 for a in joined if a == "--enable_raw_key_encryption") == 1


class TestCheckAligned:
    def test_aligned_trees_pass(self, tmp_path: Path) -> None:
        write_quality_tree(tmp_path, "720p")
        timeline = check_aligned(tmp_path / "a", tmp_path / "b", ["720p"])
        assert timeline.segment_counts == {"720p": 2}
        assert timeline.init_identical is True
        assert timeline.tfdt_aligned is True

    def test_raw_init_difference_is_canonicalised_away(self, tmp_path: Path) -> None:
        # The whole point: raw inits differ by muxer wall-clock timestamps,
        # canonicalised inits are identical, so the gate PASSES.
        write_quality_tree(tmp_path, "720p")
        raw_a = (tmp_path / "a" / "720p" / "init.mp4").read_bytes()
        raw_b = (tmp_path / "b" / "720p" / "init.mp4").read_bytes()
        assert raw_a != raw_b
        assert canonicalize_init(raw_a) == canonicalize_init(raw_b)
        check_aligned(tmp_path / "a", tmp_path / "b", ["720p"])

    def test_missing_segment_raises(self, tmp_path: Path) -> None:
        write_quality_tree(tmp_path, "720p")
        (tmp_path / "b" / "720p" / "seg_00002.m4s").unlink()
        with pytest.raises(PipelineError) as excinfo:
            check_aligned(tmp_path / "a", tmp_path / "b", ["720p"])
        assert excinfo.value.code == "PACKAGING_INIT_MISMATCH"
        assert "count mismatch" in excinfo.value.message

    def test_init_difference_beyond_timestamps_raises(self, tmp_path: Path) -> None:
        write_quality_tree(tmp_path, "720p")
        # corrupt a non-timestamp byte in b's init (the duration field)
        b_init = tmp_path / "b" / "720p" / "init.mp4"
        data = bytearray(b_init.read_bytes())
        data[-1] ^= 0xFF
        b_init.write_bytes(bytes(data))
        with pytest.raises(PipelineError) as excinfo:
            check_aligned(tmp_path / "a", tmp_path / "b", ["720p"])
        assert excinfo.value.code == "PACKAGING_INIT_MISMATCH"
        assert "init.mp4 differs" in excinfo.value.message

    def test_tfdt_mismatch_raises(self, tmp_path: Path) -> None:
        write_quality_tree(tmp_path, "720p")
        b_seg = tmp_path / "b" / "720p" / "seg_00002.m4s"
        b_seg.write_bytes(segment_bytes(9999))
        with pytest.raises(PipelineError) as excinfo:
            check_aligned(tmp_path / "a", tmp_path / "b", ["720p"])
        assert excinfo.value.code == "PACKAGING_INIT_MISMATCH"
        assert "tfdt mismatch" in excinfo.value.message

    def test_multiple_qualities(self, tmp_path: Path) -> None:
        for quality in ("720p", "480p"):
            write_quality_tree(tmp_path, quality)
        timeline = check_aligned(tmp_path / "a", tmp_path / "b", ["720p", "480p"])
        assert timeline.segment_counts == {"720p": 2, "480p": 2}


def test_segment_paths_are_numeric_not_lexicographic(tmp_path: Path) -> None:
    from vg_pipeline.shaka import _segment_paths

    for index in (1, 2, 10, 11):
        (tmp_path / f"seg_{index}.m4s").write_bytes(b"x")
    assert [p.name for p in _segment_paths(tmp_path)] == [
        "seg_1.m4s",
        "seg_2.m4s",
        "seg_10.m4s",
        "seg_11.m4s",
    ]


def test_read_playlist_durations(tmp_path: Path) -> None:
    from vg_pipeline.shaka import _read_playlist_durations

    playlist = tmp_path / "_a_720p.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXTINF:2.000000,\nseg_1.m4s\n#EXTINF:1.500000,\nseg_2.m4s\n",
        encoding="utf-8",
    )
    assert _read_playlist_durations(playlist) == [2.0, 1.5]
