"""Tests for the pure-Python MP4 box walker (init canonicalisation, tfdt)."""

from __future__ import annotations

import struct
from pathlib import Path

from vg_pipeline.mp4 import canonicalize_init, read_tfdt


def full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    body = bytes([version]) + flags.to_bytes(3, "big") + payload
    # box total = 4 (size) + 4 (type) + len(body); the size field counts all of it
    return struct.pack(">I", 8 + len(body)) + box_type + body


def container(box_type: bytes, *children: bytes) -> bytes:
    payload = b"".join(children)
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def large_container(box_type: bytes, *children: bytes) -> bytes:
    payload = b"".join(children)
    return struct.pack(">I", 1) + box_type + struct.pack(">Q", 16 + len(payload)) + payload


def build_init(times_version: int) -> bytes:
    """moov{mvhd, trak{tkhd, mdia{mdhd}}} with nonzero timestamps."""
    if times_version == 0:
        mvhd_payload = (
            struct.pack(">II", 0x11111111, 0x22222222)  # creation, modification
            + struct.pack(">II", 1000, 9999)  # timescale, duration
            + struct.pack(">I", 0x00010000)  # rate
        )
        tkhd_payload = (
            struct.pack(">II", 0x33333333, 0x44444444)  # creation, modification
            + struct.pack(">II", 7, 0)  # track_id, reserved
            + struct.pack(">I", 555)  # duration
        )
        mdhd_payload = (
            struct.pack(">II", 0x55555555, 0x66666666)  # creation, modification
            + struct.pack(">II", 48000, 14400000)  # timescale, duration
        )
    else:
        mvhd_payload = (
            struct.pack(">QQ", 0x1111111111111111, 0x2222222222222222)
            + struct.pack(">II", 1000, 9999)
            + struct.pack(">I", 0x00010000)
        )
        tkhd_payload = (
            struct.pack(">QQ", 0x3333333333333333, 0x4444444444444444)
            + struct.pack(">II", 7, 0)
            + struct.pack(">I", 555)
        )
        mdhd_payload = (
            struct.pack(">QQ", 0x5555555555555555, 0x6666666666666666)
            + struct.pack(">II", 48000, 14400000)
        )
    mvhd = full_box(b"mvhd", times_version, 0, mvhd_payload)
    tkhd = full_box(b"tkhd", times_version, 7, tkhd_payload)
    mdhd = full_box(b"mdhd", times_version, 0, mdhd_payload)
    mdia = container(b"mdia", mdhd)
    trak = container(b"trak", tkhd, mdia)
    return container(b"moov", mvhd, trak)


def box_offsets(data: bytes, marker: bytes) -> int:
    """Start offset of the box whose type is ``marker``."""
    return data.index(marker) - 4


class TestCanonicalizeInitV0:
    def test_times_zeroed(self) -> None:
        data = build_init(0)
        result = canonicalize_init(data)

        mvhd = box_offsets(result, b"mvhd")
        assert result[mvhd + 12 : mvhd + 20] == b"\x00" * 8
        tkhd = box_offsets(result, b"tkhd")
        assert result[tkhd + 12 : tkhd + 20] == b"\x00" * 8
        mdhd = box_offsets(result, b"mdhd")
        assert result[mdhd + 12 : mdhd + 20] == b"\x00" * 8

    def test_other_bytes_preserved(self) -> None:
        data = build_init(0)
        result = canonicalize_init(data)
        mvhd = box_offsets(result, b"mvhd")
        # timescale and duration untouched
        assert struct.unpack(">II", result[mvhd + 20 : mvhd + 28]) == (1000, 9999)
        tkhd = box_offsets(result, b"tkhd")
        assert struct.unpack(">I", result[tkhd + 20 : tkhd + 24]) == (7,)
        mdhd = box_offsets(result, b"mdhd")
        assert struct.unpack(">II", result[mdhd + 20 : mdhd + 28]) == (48000, 14400000)
        # versions, flags, sizes, types untouched; only the eight
        # creation/modification bytes per time box differ from the input
        assert result == _patched(data, {mvhd + 12: 8, tkhd + 12: 8, mdhd + 12: 8})

    def test_idempotent(self) -> None:
        once = canonicalize_init(build_init(0))
        assert canonicalize_init(once) == once

    def test_large_container_handled(self) -> None:
        data = large_container(b"moov", full_box(b"mvhd", 0, 0, struct.pack(">IIII", 1, 2, 3, 4)))
        result = canonicalize_init(data)
        mvhd = box_offsets(result, b"mvhd")
        assert result[mvhd + 12 : mvhd + 20] == b"\x00" * 8
        assert struct.unpack(">II", result[mvhd + 20 : mvhd + 28]) == (3, 4)

    def test_short_time_box_left_alone(self) -> None:
        # A mvhd too short to carry timestamps must not bleed zeros into
        # neighbouring bytes.
        inner = container(b"moov", full_box(b"mvhd", 0, 0, b"\x01\x02"))
        assert canonicalize_init(inner) == inner


class TestCanonicalizeInitV1:
    def test_times_zeroed(self) -> None:
        data = build_init(1)
        result = canonicalize_init(data)
        mvhd = box_offsets(result, b"mvhd")
        assert result[mvhd + 12 : mvhd + 28] == b"\x00" * 16
        tkhd = box_offsets(result, b"tkhd")
        assert result[tkhd + 12 : tkhd + 28] == b"\x00" * 16
        mdhd = box_offsets(result, b"mdhd")
        assert result[mdhd + 12 : mdhd + 28] == b"\x00" * 16

    def test_other_bytes_preserved(self) -> None:
        data = build_init(1)
        result = canonicalize_init(data)
        mvhd = box_offsets(result, b"mvhd")
        assert struct.unpack(">II", result[mvhd + 28 : mvhd + 36]) == (1000, 9999)


def _patched(data: bytes, zero_ranges: dict[int, int]) -> bytes:
    """Reference implementation: zero the given (offset, length) ranges."""
    buf = bytearray(data)
    for start, length in zero_ranges.items():
        buf[start : start + length] = b"\x00" * length
    return bytes(buf)


class TestReadTfdt:
    def _segment(self, version: int, base: int) -> bytes:
        if version == 0:
            tfdt = full_box(b"tfdt", 0, 0, struct.pack(">I", base))
        else:
            tfdt = full_box(b"tfdt", 1, 0, struct.pack(">Q", base))
        traf = container(b"traf", tfdt, full_box(b"sdtp", 0, 0, b"\x16\x00\x00\x00"))
        moof = container(b"moof", traf)
        styp = container(b"styp", b"msdh" + b"\x00" * 4)
        return styp + moof

    def test_v0(self, tmp_path: Path) -> None:
        path = tmp_path / "seg_00001.m4s"
        path.write_bytes(self._segment(0, 4242))
        assert read_tfdt(path) == 4242

    def test_v1_64bit(self, tmp_path: Path) -> None:
        path = tmp_path / "seg_00002.m4s"
        base = 5_000_000_000  # beyond 32 bits
        path.write_bytes(self._segment(1, base))
        assert read_tfdt(path) == base

    def test_first_moof_wins(self, tmp_path: Path) -> None:
        data = self._segment(0, 111) + self._segment(0, 222)
        path = tmp_path / "seg.m4s"
        path.write_bytes(data)
        assert read_tfdt(path) == 111

    def test_no_tfdt_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "seg.m4s"
        path.write_bytes(container(b"moof", container(b"traf")))
        assert read_tfdt(path) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert read_tfdt(tmp_path / "absent.m4s") is None

    def test_init_mp4_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "init.mp4"
        path.write_bytes(build_init(0))
        assert read_tfdt(path) is None
