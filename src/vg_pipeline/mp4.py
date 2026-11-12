"""Small ISO-BMFF (MP4) box helpers, pure Python with no external deps.

ISO-BMFF is the MP4 container format: a file is a sequence of boxes, each
starting with a big-endian 4-byte ``size`` and a 4-byte type. A size of 1
means a 64-bit ``largesize`` follows the type; a size of 0 means the box
extends to end of file.

Two jobs (CONTRACT.md section 6):

* :func:`canonicalize_init` zeroes ``creation_time``/``modification_time`` in
  ``mvhd``, ``tkhd`` and ``mdhd`` inside ``moov``, so two Shaka runs that only
  differ by wall-clock timestamps become byte-identical.
* :func:`read_tfdt` reads ``baseMediaDecodeTime`` from the first
  ``moof/traf/tfdt`` for the aligned-timeline gate.
"""

from __future__ import annotations

from pathlib import Path

# Leaf boxes whose first two fields after version+flags are
# creation_time / modification_time.
_TIME_BOXES = frozenset({"mvhd", "tkhd", "mdhd"})
# Container boxes we descend into to find them.
_CONTAINER_BOXES = frozenset({"moov", "trak", "mdia"})


def _iter_boxes(data: bytes | bytearray, start: int, end: int):
    """Yield ``(offset, size, type_str, header_size)`` for each direct child box.

    ``offset + header_size`` is where the box payload (or, for a container,
    its child boxes) starts. Malformed input stops the walk instead of
    raising: an unparseable init must not crash the packaging stage here (the
    byte-equality gate below will simply see different bytes).
    """
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos : pos + 4], "big")
        box_type = bytes(data[pos + 4 : pos + 8]).decode("latin-1")
        header = 8
        if size == 1:
            # 64-bit largesize directly after the type field.
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:
            # Box extends to the end of the enclosing range.
            size = end - pos
        if size < header or pos + size > end:
            return
        yield pos, size, box_type, header
        pos += size


def _zero_full_box_times(buf: bytearray, box_start: int, box_size: int) -> None:
    """Zero creation_time and modification_time of one full box in place.

    Full-box layout: ``size``(4) ``type``(4) ``version``(1) ``flags``(3),
    then version 0 = two 32-bit times, version 1 = two 64-bit times.
    """
    if box_size < 12:
        return
    version = buf[box_start + 8]
    base = box_start + 12  # first field after version+flags
    end = base + 16 if version == 1 else base + 8
    if end > box_start + box_size:
        # Box too short to hold both timestamps; leave it untouched rather
        # than overwrite bytes of a following box.
        return
    buf[base:end] = b"\x00" * (end - base)


def _walk_and_zero(buf: bytearray, start: int, end: int) -> None:
    for pos, size, box_type, header in list(_iter_boxes(buf, start, end)):
        if box_type in _TIME_BOXES:
            _zero_full_box_times(buf, pos, size)
        elif box_type in _CONTAINER_BOXES:
            _walk_and_zero(buf, pos + header, pos + size)


def canonicalize_init(data: bytes) -> bytes:
    """Return a copy of ``data`` with all init times zeroed.

    ``mvhd`` (movie header), each ``tkhd`` (track header) and each ``mdhd``
    (media header) under ``moov`` get ``creation_time`` and
    ``modification_time`` set to 0 for both version 0 (32-bit) and version 1
    (64-bit) boxes. Every other byte is preserved.
    """
    buf = bytearray(data)
    _walk_and_zero(buf, 0, len(buf))
    return bytes(buf)


def _read_tfdt_bytes(data: bytes) -> int | None:
    """Find the first ``moof``/``traf``/``tfdt`` and read its decode time."""
    for moof_pos, moof_size, moof_type, moof_header in _iter_boxes(data, 0, len(data)):
        if moof_type != "moof":
            continue
        for traf_pos, traf_size, traf_type, traf_header in _iter_boxes(
            data, moof_pos + moof_header, moof_pos + moof_size
        ):
            if traf_type != "traf":
                continue
            for tfdt_pos, tfdt_size, tfdt_type, _tfdt_header in _iter_boxes(
                data, traf_pos + traf_header, traf_pos + traf_size
            ):
                if tfdt_type != "tfdt" or tfdt_size < 12:
                    continue
                version = data[tfdt_pos + 8]
                base = tfdt_pos + 12  # after size, type, version+flags
                if version == 1:
                    return int.from_bytes(data[base : base + 8], "big")
                return int.from_bytes(data[base : base + 4], "big")
        # Only the first moof is consulted, matching "first tfdt" semantics.
        return None
    return None


def read_tfdt(path: Path) -> int | None:
    """Read ``baseMediaDecodeTime`` of the first ``tfdt`` in the segment file.

    Version 0 carries a 32-bit value, version 1 a 64-bit value. Returns
    ``None`` when the file has no ``tfdt`` (or is not readable media).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return _read_tfdt_bytes(data)
