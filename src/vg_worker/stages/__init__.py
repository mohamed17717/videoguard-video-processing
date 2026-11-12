"""Worker pipeline stages: thin wrappers around :mod:`vg_pipeline`.

One module per webhook stage:

* :mod:`vg_worker.stages.validate`  -> ``validated``
* :mod:`vg_worker.stages.generate`  -> ``masters_ready``
* :mod:`vg_worker.stages.ladder`    -> encoding prep (no webhook of its own;
  quality reports happen after the encrypted renditions land)
* :mod:`vg_worker.stages.package`   -> ``rendition_ready`` per quality, then
  ``packaged``
* :mod:`vg_worker.stages.finalize`  -> ``done`` payload and ``_DONE`` body

Audit note: the renditions published under ``renditions/<job>/`` are the
ENCRYPTED packager outputs only. The pre-encryption ladder outputs live in
the local work directory and never touch the Bunny-readable prefix, so a
packaging failure can never leave clear segments on the CDN.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from vg_pipeline.contracts import JobMessage, PipelineConfig, ProbeResult
from vg_pipeline.logging import get_logger

from vg_worker.config import WorkerConfig
from vg_worker.s3io import S3IO, storage_class_for_object
from vg_worker.webhooks import Emitter

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping  # noqa: F401 — annotations only

log = get_logger("vg_worker.stages")

# Types for the injectable collaborators.
CallableRunner = Callable[..., object]
AnyPost = Callable[[str, bytes], "tuple[int, bytes]"]
AnyClock = Callable[[], float]


@dataclass
class StageContext:
    """Everything the stage functions need, with injectable collaborators.

    The worker :func:`vg_worker.main.run` builds this once per message and
    passes it to every stage; tests fill it with fakes and run stages
    directly.
    """

    job: JobMessage
    media: PipelineConfig
    aws: WorkerConfig
    s3: S3IO
    emitter: Emitter
    work: Path = Path("/work")
    # Docker argv runner for the validator + generator containers.
    container_runner: "CallableRunner | None" = None
    # Runner for the local pinned ffmpeg (ladder encodes, thumbs, audio).
    ffmpeg_runner: "CallableRunner | None" = None
    # Runner for the host Shaka packager binary.
    packager_runner: "CallableRunner | None" = None
    # Injectable http_post for the Axinom key request (None = urllib default).
    axinom_post: "AnyPost | None" = None
    # Instance identity + image pin, used for logs and manifest.json.
    instance_id: str = ""
    ami_id: str = ""
    generator_image: str = ""
    packager_version: str = ""
    ffmpeg_version: str = ""
    # Stage bookkeeping: which webhook stage the pipeline is currently in
    # (used as ``qualifier`` of a ``failed`` webhook).
    stage: str = "receive"
    # Mutable results handed from stage to stage.
    masters: dict[str, Path] = field(default_factory=dict)
    source_sha256: str = ""
    completion: dict[str, object] = field(default_factory=dict)
    audio_path: "Path | None" = None
    thumbs: list[Path] = field(default_factory=list)
    # Pre-encryption H.264 ladder outputs: (variant, quality) -> local path.
    encoded: dict[tuple[str, str], Path] = field(default_factory=dict)
    # key -> uploaded bytes for every rendition-side published object.
    published_bytes: dict[str, int] = field(default_factory=dict)

    # -- S3 key builders (CONTRACT.md section 1) --------------------------

    def key(self, prefix_name: str, *parts: str) -> str:
        """``<prefix><job_id>/<parts...>`` for a configured prefix.

        A trailing empty part (``key("renditions", "audio", "")``) keeps the
        trailing slash: it returns a PREFIX rather than an object key.
        """
        base: str = getattr(self.aws.prefixes, prefix_name)
        if not parts:
            return f"{base}{self.job.job_id}/"
        if parts[-1] == "":
            segments = [part.strip("/") for part in parts[:-1]]
            return f"{base}{self.job.job_id}/{'/'.join(segments)}/"
        segments = [part.strip("/") for part in parts if part.strip("/")]
        return f"{base}{self.job.job_id}/{'/'.join(segments)}"

    def incoming_key(self) -> str:
        """The uploaded original: ``incoming/<job_id>/source.mp4``."""
        return f"{self.aws.prefixes.incoming}{self.job.job_id}/source.mp4"

    # -- storage-class choice (per job, from the sidecar retention) -------

    def class_of(self, kind: str) -> str:
        """Storage class for one object kind: ``cmaf``/``master``/``standard``."""
        return storage_class_for_object(kind, self.job.retention)


@dataclass
class Keepalive:
    """The 300 s cadence shared by the SQS heartbeat and the lock renewal."""

    interval_sec: float
    clock: "AnyClock"
    callback: "Callable[[], None]"

    def __post_init__(self) -> None:
        self._last: float = self.clock()
        self._started: bool = True

    def due(self) -> bool:
        """True once a full interval has passed since the last firing."""
        return self.clock() - self._last >= self.interval_sec

    def tick(self) -> bool:
        """Run the callback when due; returns whether it fired."""
        if not self.due():
            return False
        self._last = self.clock()
        self.callback()
        return True


def streaming_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of a file, streamed in chunks (never loads it into RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(dest: Path, payload: bytes, mode: int = 0o444) -> None:
    """Write ``payload`` to ``dest`` via temp file + rename, then chmod.

    The default mode 0444 marks an uploaded source read-only on disk, which
    mirrors the ``:ro`` mounts given to the containers.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, dest)
        os.chmod(dest, mode)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def probe_summary(probe: ProbeResult) -> dict[str, object]:
    """The probe summary used in the ``validated`` webhook payload."""
    return {
        "codec": probe.codec,
        "width": probe.width,
        "height": probe.height,
        "fps": round(probe.fps, 3),
        "duration_sec": round(probe.duration_sec, 3),
        "has_audio": probe.has_audio,
        "container": probe.container,
    }


# The stage modules are part of the package surface: main() calls
# ``stages.validate.run_validate(...)`` etc., so they must be importable as
# attributes of the package. They import StageContext from THIS module, which
# is why the imports sit at the END (after the definitions above).
from . import finalize as finalize  # noqa: E402,F401
from . import generate as generate  # noqa: E402,F401
from . import ladder as ladder  # noqa: E402,F401
from . import package as package  # noqa: E402,F401
from . import validate as validate  # noqa: E402,F401
