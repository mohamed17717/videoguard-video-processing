"""Error type shared by every pipeline module.

One exception class with a stable machine-readable ``code`` so the worker can
route failures deterministically (see CONTRACT.md section 2): deterministic
codes go straight to the DLQ, unknown codes are treated as transient.
"""

from __future__ import annotations

# The full set of failure codes named in CONTRACT.md. Values are stable; the
# worker and the backend switch on these strings.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "INPUT_CONTRACT",
        "CLAMAV",
        "GENERATOR_EXIT_2",
        "PACKAGING_INIT_MISMATCH",
        "SIDECAR_INVALID",
        "ENCODER_UNAVAILABLE",
        "DISK_FULL",
        "ALIGNED_TIMELINE",
        "CENC_FAILED",
        "MANIFEST_FAILED",
        "LOCK_CONFLICT",
    }
)


class PipelineError(Exception):
    """Pipeline failure with a stable code and a human message.

    ``code`` is one of :data:`ERROR_CODES`. It travels into the ``failed``
    webhook payload and decides whether the SQS message goes to the DLQ at
    once (deterministic codes) or is left for redelivery (transient).
    ``message`` is safe to log: never put a secret into it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        return f"PipelineError(code={self.code!r}, message={self.message!r})"
