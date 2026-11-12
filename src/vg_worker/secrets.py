"""Secrets Manager access and the temporary watermark secret file.

Secrets are fetched with ``secretsmanager:GetSecretValue`` and held in
process memory only (CONTRACT.md section 5). Expected JSON shapes:

* watermark: ``{"secret": "..."}``
* webhook:   ``{"secret": "..."}``
* Axinom:    ``{"signing_key_hex": ..., "signing_iv_hex": ..., "endpoint":
             ..., "provider_name": ..., "scheme": "cbcs"}``

For the generator the watermark secret becomes a file on the host tmpfs
``/run/vg-secrets`` (mounted tmpfs by the AMI), mode 0400, owner 10001:10001.
It is bind-mounted read-only into the container and unlinked afterwards.
NEVER write the secret to ``/work``, S3, the image, environment variables or
logs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from vg_pipeline.logging import get_logger

log = get_logger("vg_worker.secrets")

# Directory the AMI mounts as tmpfs; the secret file lives there briefly.
SECRETS_DIR = Path("/run/vg-secrets")
WATERMARK_FILE_NAME = "watermark"

# The worker needs all three logical secrets; anything missing is fatal.
_REQUIRED: dict[str, Sequence[str]] = {
    "watermark": ("secret",),
    "webhook": ("secret",),
    "axinom": ("signing_key_hex", "signing_iv_hex", "endpoint", "provider_name"),
}

# Host owner of the tmpfs secret file: the generator container's non-root user.
_OWNER_UID = 10001
_OWNER_GID = 10001


class SecretsClient(Protocol):
    """The subset of a boto3 Secrets Manager client the worker needs."""

    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]: ...


class FetchedSecrets(dict):
    """Parsed secrets keyed by logical name plus a VersionId per secret.

    The watermark entry carries an extra key ``version_id`` (added by this
    module) so the manifest can pin the secret version without logging or
    storing the value itself.
    """

    def version_of(self, name: str) -> str:
        """The Secrets Manager VersionId for one logical secret name."""
        return str(self.get(name, {}).get("version_id", ""))


def _secret_string(response: Mapping[str, Any], arn: str) -> str:
    if not isinstance(response, Mapping):
        raise ValueError(f"secret {arn} returned an unexpected response type")
    if "SecretString" in response:
        return str(response["SecretString"])
    if "SecretBinary" in response:
        return bytes(response["SecretBinary"]).decode("utf-8")
    raise ValueError(f"secret {arn} has neither SecretString nor SecretBinary")


def _validate_shape(name: str, secret: dict, arn: str) -> None:
    """Fail when a required field is missing; the error mentions only the arn."""
    missing = [field for field in _REQUIRED[name] if not secret.get(field)]
    if missing:
        raise ValueError(f"secret {arn} ({name}) is missing required field(s): {', '.join(missing)}")


def fetch_secrets(client: SecretsClient, arns: Mapping[str, str]) -> FetchedSecrets:
    """Fetch every secret ARN and validate the JSON shape.

    ``arns`` maps logical name -> ARN; all three names (``watermark``,
    ``webhook``, ``axinom``) are required. The secret VALUES never reach the
    log: errors quote the ARN only, never the parsed content.
    """
    for name in _REQUIRED:
        if not arns.get(name):
            raise ValueError(f"no Secrets Manager ARN configured for {name}")
    fetched = FetchedSecrets()
    for name, arn in arns.items():
        if name not in _REQUIRED:
            raise ValueError(f"unknown logical secret name {name!r}")
        try:
            response = client.get_secret_value(SecretId=arn)
        except Exception as exc:  # noqa: BLE001 — surface name + cause type only
            raise ValueError(f"cannot fetch secret {arn} ({name}): {type(exc).__name__}") from exc
        try:
            content = json.loads(_secret_string(response, arn))
        except ValueError as exc:
            raise ValueError(f"secret {arn} ({name}) is not valid JSON") from exc
        if not isinstance(content, dict):
            raise ValueError(f"secret {arn} ({name}) is not a JSON object")
        _validate_shape(name, content, arn)
        content["version_id"] = str(response.get("VersionId", ""))
        fetched[name] = content
    return fetched


def _geteuid() -> int:
    """Indirection so tests can simulate the effective uid."""
    return os.geteuid()


def _force_owner(path: Path) -> None:
    """Set the tmpfs owner to 10001:10001 (only possible as root)."""
    if _geteuid() != 0:
        return
    os.chown(path, _OWNER_UID, _OWNER_GID)


def write_watermark_tmpfs(secret: str, path: Path) -> None:
    """Write the watermark secret to ``path`` on tmpfs: 0400, owned 10001:10001.

    The parent directory must already be a writable tmpfs mount (the AMI
    mounts ``/run/vg-secrets``). The write is atomic (temp file + rename) so a
    partial file can never be mounted into the generator container. The path
    is opened exclusively so a leftover file from a previous run cannot be
    overwritten silently.
    """
    if not secret:
        raise ValueError("watermark secret is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        # O_EXCL prevents silently replacing a live file; O_NOFOLLOW prevents
        # someone pre-placing a symlink to a second file.
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(secret.encode("utf-8"))
        os.replace(tmp, path)
        os.chmod(path, 0o400)
        _force_owner(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    log.info("watermark secret staged", extra={"path": str(path), "bytes": len(secret)})


@contextmanager
def watermark_secret_file(secret: str, directory: PathLike = SECRETS_DIR) -> Iterator[Path]:
    """Stage the secret on tmpfs and ALWAYS unlink it on exit.

    The yielded path is what the generator container bind-mounts at
    ``/run/secrets/watermark``. Cleanup runs even when the container fails.
    """
    path = Path(directory) / WATERMARK_FILE_NAME
    write_watermark_tmpfs(secret, path)
    try:
        yield path
    finally:
        try:
            path.unlink()
            log.info("watermark secret unlinked", extra={"path": str(path)})
        except FileNotFoundError:
            pass
