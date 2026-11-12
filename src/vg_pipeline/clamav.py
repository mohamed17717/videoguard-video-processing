"""ClamAV scanning of the uploaded file (worker ``validated`` stage).

A plain ``clamscan`` run: exit 0 means clean, exit 1 means something was
found, exit 2 means the scan itself failed. See ``man clamscan``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .contracts import ScanResult
from .errors import PipelineError
from .logging import get_logger

log = get_logger("clamav")

RunLike = Callable[..., subprocess.CompletedProcess]


def build_scan_command(path: Path, clamscan_bin: str = "clamscan") -> list[str]:
    """Build the ``clamscan`` argument list for one file scan.

    ``--no-summary`` keeps the output short and ``--infected`` prints only
    infected files, so a clean scan produces no output at all.
    """
    return [clamscan_bin, "--no-summary", "--infected", str(path)]


def scan(path: Path, run: RunLike = subprocess.run) -> ScanResult:
    """Scan ``path`` and return the result.

    Exit codes: 0 -> clean, 1 -> infected (``clean=False``), 2 or any runner
    failure -> :class:`PipelineError` with code ``CLAMAV``. ``output`` holds
    ClamAV's combined stdout+stderr text; it contains no secrets.
    """
    cmd = build_scan_command(path)
    log.debug("clamav scan start", extra={"path": str(path)})
    try:
        completed = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise PipelineError("CLAMAV", f"cannot run clamscan: {exc}") from exc
    code = getattr(completed, "returncode", None)
    output = ((getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")).strip()
    if code == 0:
        log.info("clamav scan clean", extra={"path": str(path)})
        return ScanResult(clean=True, output=output)
    if code == 1:
        log.warning("clamav scan found malware", extra={"path": str(path)})
        return ScanResult(clean=False, output=output)
    raise PipelineError("CLAMAV", f"clamscan failed with exit {code}: {output[-500:]}")
