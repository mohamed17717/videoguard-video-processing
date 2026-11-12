"""Golden vector + property tests for the A/B pattern (CONTRACT.md section 7).

The algorithm is the CONTRACT's own: ``digest = HMAC_SHA256(seed.encode(),
f"vg-ab-{n}".encode())``; ``variant = "a" if digest[0] & 1 == 0 else "b"``.
Every expected value here is computed INLINE with a second, independent
``hmac.new(...)`` expression, so a bug in :mod:`vg_pipeline.ab_pattern`
cannot hide behind the tests. No network, no fixtures.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from vg_pipeline import ab_pattern
from vg_pipeline.ab_pattern import pattern, pattern_for_count

SEED = "vg-test-seed-0001"

# Hard-coded golden vector (CONTRACT.md section 7 requirement). Verified
# against the exact contract formula; see the module docstring for the
# first digest bytes.
GOLDEN_VARIANTS = ["b", "b", "a", "a", "a", "a", "a", "a"]
GOLDEN_DIGEST_PREFIXES = {
    0: "1d4c3cffe1118ecc",
    1: "9f4e0305e14adec3",
    2: "e85502770d355712",
    3: "faffb0faf5efbb1c",
}


def independent_variant(seed: str, n: int) -> str:
    """The contract formula written out again, without importing the module."""
    digest = hmac.new(seed.encode("utf-8"), f"vg-ab-{n}".encode("utf-8"), hashlib.sha256).digest()
    return "a" if digest[0] & 1 == 0 else "b"


def test_golden_vector_seed_n_0_to_7() -> None:
    for n, expected in enumerate(GOLDEN_VARIANTS):
        assert pattern(SEED, n) == expected, f"n={n}"
    assert pattern_for_count(SEED, len(GOLDEN_VARIANTS)) == GOLDEN_VARIANTS


def test_golden_vector_digest_bytes_are_pinned() -> None:
    """The first digest bytes are part of the golden vector: hex prefixes."""
    for n, prefix in GOLDEN_DIGEST_PREFIXES.items():
        digest = hmac.new(SEED.encode("utf-8"), f"vg-ab-{n}".encode("utf-8"), hashlib.sha256).digest()
        assert digest.hex()[:16] == prefix
        # the variant bit comes from that first byte's lowest bit
        assert pattern(SEED, n) == ("a" if int(prefix[:2], 16) & 1 == 0 else "b")


def test_same_input_always_same_variant() -> None:
    """Determinism: repeating the call never changes the verdict."""
    for seed in (SEED, "another-seed", "", "🎉-seed"):
        for n in (0, 1, 2, 7, 12345):
            assert pattern(seed, n) == pattern(seed, n)
            assert pattern(seed, n) == pattern_for_count(seed, n + 1)[n]


@pytest.mark.parametrize(
    "left,right",
    [(SEED, "vg-test-seed-0002"), (SEED, "vg-test-seed-000"), ("a", "b"), ("", "x")],
)
def test_different_seeds_disagree_somewhere(left: str, right: str) -> None:
    """Independence: two seeds must not always pick the same variant."""
    count = 32
    left_variants = pattern_for_count(left, count)
    right_variants = pattern_for_count(right, count)
    assert left_variants != right_variants, "different seeds produced identical patterns"
    # and each seed's own series stays consistent with single calls
    assert left_variants == [pattern(left, n) for n in range(count)]


def test_negative_n_still_follows_the_exact_formula() -> None:
    """Out-of-domain ``n`` is not special-cased: the formula stays exact.

    ``f"vg-ab-{n}"`` of a negative number is simply the text
    ``vg-ab--3``; the implementation must match an independent
    computation of exactly that (and the Deno edge does the same).
    """
    for n in (-1, -2, -3):
        assert pattern(SEED, n) == independent_variant(SEED, n)
        assert pattern(SEED, n) in {"a", "b"}


def test_count_boundaries() -> None:
    assert pattern_for_count(SEED, 0) == []
    assert pattern_for_count(SEED, -5) == []  # no sessions, no variants
    assert pattern_for_count(SEED, 1) == [GOLDEN_VARIANTS[0]]
    assert pattern_for_count(SEED, 8) == GOLDEN_VARIANTS


def test_matches_independent_implementation_over_a_range() -> None:
    """The module agrees with a second, inline ``hmac.new`` computation."""
    for n in range(0, 128):
        assert pattern(SEED, n) == independent_variant(SEED, n), f"n={n}"
    for other in ("seed-a", "seed-b", "AUcHv7Pw0N44"):
        for n in range(0, 32):
            assert pattern(other, n) == independent_variant(other, n), f"seed={other} n={n}"


def test_package_export_is_the_same_function() -> None:
    """The additive ``vg_pipeline.pattern`` export must be the real thing."""
    from vg_pipeline import pattern as exported

    assert exported is pattern
    assert exported is ab_pattern.pattern
    assert exported(SEED, 0) == "b"
