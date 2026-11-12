"""The A/B variant decision for playback sessions (CONTRACT.md section 7).

Every playback session watches ONE variant of the watermarked video
(``a`` or ``b``), chosen from a session ``seed``. Two properties matter:

* the SAME seed must always pick the SAME variant, so a page refresh
  keeps showing the same picture;
* different seeds must spread over ``a`` and ``b``, so a leak points at
  exactly one session, not at everyone.

The exact rule from CONTRACT.md section 7:

    digest = HMAC_SHA256(seed.encode(), f"vg-ab-{n}".encode())
    variant = "a" if digest[0] & 1 == 0 else "b"

``HMAC_SHA256`` is a keyed hash: the ``seed`` is the key, ``vg-ab-<n>``
is the message, and ``digest[0]`` is the first byte of the 32-byte
result. Its lowest bit is a uniform coin flip tied to the seed. ``n`` is
the session counter (0-based); DASH uses only ``pattern(seed, 0)``,
while HLS may substitute per segment with ``n = 1, 2, ...``.

Golden vector (hard-coded in ``tests/pipeline/test_ab_pattern.py``):
seed = ``"vg-test-seed-0001"``, n = 0..7 →
``["b", "b", "a", "a", "a", "a", "a", "a"]``. First digest bytes (hex):
n0 = ``1d4c3cffe1118ecc``, n1 = ``9f4e0305e14adec3``,
n2 = ``e85502770d355712``, n3 = ``faffb0faf5efbb1c``.

Only the Python standard library (``hmac``/``hashlib``) is used, with
UTF-8 encoding everywhere, so the Python and Deno implementations stay
byte-identical.
"""

from __future__ import annotations

import hashlib
import hmac


def pattern(seed: str, n: int) -> str:
    """Return ``"a"`` or ``"b"`` for session ``n`` under ``seed``.

    Implements CONTRACT.md section 7 exactly:
    ``digest = HMAC_SHA256(seed.encode(), f"vg-ab-{n}".encode())`` and
    ``variant = "a" if digest[0] & 1 == 0 else "b"``. Both strings are
    encoded as UTF-8; the seed is the HMAC key. Works for any integer
    ``n`` (the message is simply the text ``vg-ab-<n>``).
    """
    digest = hmac.new(seed.encode("utf-8"), f"vg-ab-{n}".encode("utf-8"), hashlib.sha256).digest()
    return "a" if digest[0] & 1 == 0 else "b"


def pattern_for_count(seed: str, count: int) -> list[str]:
    """The variants for sessions ``0 .. count-1``; ``count <= 0`` is empty."""
    return [pattern(seed, n) for n in range(count)]
