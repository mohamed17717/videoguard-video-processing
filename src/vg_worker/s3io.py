"""S3 wrapper: every worker S3 call goes through :class:`S3IO`.

The real storage client is injected as a *factory*: ``S3IO`` creates the
client lazily on first use, so importing this module never touches boto3.
Tests pass a fake factory and run fully offline.

Storage classes are chosen PER JOB from the sidecar ``retention`` field
(CONTRACT.md section 1) — there is no global setting:

* ``retention=archive`` -> the CMAF renditions, masters and inits are placed
  in ``GLACIER_IR`` from day 0 and the original ``incoming`` object is
  additionally tagged ``processed=true``;
* ``retention=ttl30`` -> everything stays ``STANDARD`` (never transitioned to
  Glacier, because the 90-day archive minimum would outlive the 30-day TTL).

Every written object carries the tag ``vg-retention=<retention>``.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, Union

PathLike = Union[str, "os.PathLike[str]"]


def storage_class_for_object(kind: str, retention: str) -> str:
    """Choose the S3 storage class for one object kind and one job retention.

    ``kind`` is one of ``cmaf`` (segments, init files, audio tree), ``master``
    (watermarked A/B masters) or ``standard`` (reports, canonical manifests,
    thumbnails — always ``STANDARD``).

    ``retention`` is ``archive`` or ``ttl30``. Raises ``ValueError`` for an
    unknown retention so a typo cannot silently change archival behaviour.
    """
    if retention not in _RETENTION_CLASSES:
        raise ValueError(f"unknown retention {retention!r}; expected one of {sorted(_RETENTION_CLASSES)}")
    if kind == "standard":
        return "STANDARD"
    if kind in ("cmaf", "master"):
        return _RETENTION_CLASSES[retention]
    raise ValueError(f"unknown object kind {kind!r}; expected cmaf | master | standard")


# retention -> storage class for the long-lived media objects.
_RETENTION_CLASSES: dict[str, str] = {"archive": "GLACIER_IR", "ttl30": "STANDARD"}

# Tag key/value names, per CONTRACT.md section 1.
TAG_RETENTION = "vg-retention"
TAG_PROCESSED = "processed"


def make_tags(retention: str, processed: bool = False) -> dict[str, str]:
    """The object tags each output must carry."""
    tags = {TAG_RETENTION: retention}
    if processed:
        tags[TAG_PROCESSED] = "true"
    return tags


def encode_tagging(tags: Mapping[str, str]) -> str:
    """Serialise tags into the URL-encoded ``Tagging`` string of PutObject."""
    return urllib.parse.urlencode(dict(tags))


def tagging_tag_set(tags: Mapping[str, str]) -> dict[str, list[dict[str, str]]]:
    """Serialise tags into the boto3 ``Tagging`` value of PutObjectTagging."""
    return {"TagSet": [{"Key": key, "Value": value} for key, value in sorted(dict(tags).items())]}


class S3Client(Protocol):
    """The subset of a boto3 S3 client the worker needs.

    Method names and keyword names match boto3 exactly, so a real client
    works unchanged and tests provide a small fake.
    """

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def upload_fileobj(self, fileobj: Any, bucket: str, key: str, **kwargs: Any) -> Any: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object_tagging(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _transfer_config() -> Any:
    """Bounded-memory multipart transfer settings (8 MiB chunks)."""
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=8 * 1024 * 1024,
        use_threads=True,
    )


def _http_status(exc: Exception) -> int | None:
    """Read the HTTP status a boto3-style error carries (None when absent)."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            status = metadata.get("HTTPStatusCode")
            if isinstance(status, int):
                return status
    return None


class S3IO:
    """Thin, testable facade over one bucket.

    ``client_factory`` creates the storage client on first use (tests inject a
    fake; production injects ``boto3.client("s3")``).
    """

    def __init__(self, client_factory: Callable[[], S3Client], bucket: str) -> None:
        if not bucket:
            raise ValueError("S3IO needs a bucket name")
        self._factory: Callable[[], S3Client] = client_factory
        self._bucket: str = bucket
        self._client: S3Client | None = None

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def client(self) -> S3Client:
        if self._client is None:
            self._client = self._factory()
        return self._client

    # -- plain objects ---------------------------------------------------

    def get_object(self, key: str) -> bytes:
        """Read one object body. Missing objects raise the client's error."""
        response = self.client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        return body.read() if hasattr(body, "read") else bytes(body)

    def get_object_with_etag(self, key: str) -> tuple[bytes, str]:
        """Read body + ETag from ONE GET (no TOCTOU between two requests).

        The lock takeover must compare against the ETag of the exact body it
        inspected; a separate HEAD could observe a newer lock.
        """
        response = self.client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        data = body.read() if hasattr(body, "read") else bytes(body)
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise ValueError(f"object {key} returned no ETag")
        return data, etag

    def get_json_or_none(self, key: str) -> dict[str, Any] | None:
        """Parsed JSON object, or ``None`` on a genuine 404 (never on 403)."""
        try:
            raw = self.get_object(key)
        except Exception as exc:  # noqa: BLE001 — one place: any client error shape
            if _http_status(exc) == 404:
                return None
            raise
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"object {key} is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"object {key} is not a JSON object")
        return parsed

    def put_file(
        self,
        key: str,
        path: PathLike,
        *,
        storage_class: str,
        tags: Mapping[str, str],
    ) -> None:
        """Stream one local file with boto3's bounded-memory multipart manager.

        ``read_bytes()`` + one ``PutObject`` loads the whole object into RAM and
        hits S3's single-PUT limit for large masters.
        """
        extra = {"StorageClass": storage_class, "Tagging": encode_tagging(tags)}
        with open(path, "rb") as handle:
            self.client.upload_fileobj(
                handle,
                self._bucket,
                key,
                ExtraArgs=extra,
                Config=_transfer_config(),
            )

    def put_object(
        self,
        key: str,
        data: bytes,
        *,
        storage_class: str,
        tags: Mapping[str, str],
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str | None:
        """Write one object; returns its ETag.

        Disallowed by AWS: ``if_none_match`` and ``if_match`` are mutually
        exclusive. Tagging and StorageClass follow the CONTRACT.md rules.
        """
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "StorageClass": storage_class,
            "Tagging": encode_tagging(tags),
        }
        if if_none_match and if_match:
            raise ValueError("if_none_match and if_match are mutually exclusive")
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        if if_match:
            kwargs["IfMatch"] = if_match
        response = self.client.put_object(**kwargs)
        etag = response.get("ETag") if isinstance(response, Mapping) else None
        return etag if isinstance(etag, str) else None

    def head_object(self, key: str) -> Mapping[str, Any]:
        """Metadata of one object; raises when it does not exist."""
        return self.client.head_object(Bucket=self._bucket, Key=key)

    def put_tagging(self, key: str, tags: Mapping[str, str]) -> None:
        """Replace the whole tag set of an existing object (e.g. the upload)."""
        self.client.put_object_tagging(
            Bucket=self._bucket,
            Key=key,
            Tagging=tagging_tag_set(tags),
        )

    # -- job markers -----------------------------------------------------

    def conditional_lock(self, key: str, body: bytes) -> tuple[bool, str | None]:
        """Write ``_LOCK`` only when it does not exist yet (IfNoneMatch: *).

        Returns ``(ok, etag)``. A 412 (PreconditionFailed) means another
        worker instance already holds the lock -> ``(False, None)``.
        """
        try:
            etag = self.put_object(key, body, storage_class="STANDARD", tags={"purpose": "lock"}, if_none_match=True)
        except Exception as exc:  # noqa: BLE001 — one place: any client error shape
            if _http_status(exc) == 412:
                return False, None
            raise
        return True, etag

    def read_lock(self, key: str) -> dict[str, Any]:
        """Parse the current ``_LOCK`` body (raises when unreadable)."""
        return json.loads(self.get_object(key).decode("utf-8"))

    def renew_lock(self, key: str, etag: str, body: bytes) -> tuple[bool, str | None]:
        """Overwrite ``_LOCK`` only while we still own its exact ETag.

        Returns ``(ok, new_etag)``; a 412 means the lock changed underneath
        us and the caller must abort.
        """
        try:
            new_etag = self.put_object(key, body, storage_class="STANDARD", tags={"purpose": "lock"}, if_match=etag)
        except Exception as exc:  # noqa: BLE001
            if _http_status(exc) == 412:
                return False, None
            raise
        return True, new_etag

    def expire_lock(self, key: str, etag: str) -> bool:
        """Mark our ``_LOCK`` expired so a redelivery can take it over.

        The conditional write leaves another worker's newer lock untouched.
        ``False`` means the lock changed underneath us (nothing to do).
        """
        body = json.dumps({"expires_at": 0}, sort_keys=True).encode("utf-8")
        try:
            self.put_object(
                key,
                body,
                storage_class="STANDARD",
                tags={"purpose": "lock"},
                if_match=etag,
            )
        except Exception as exc:  # noqa: BLE001
            if _http_status(exc) in (404, 412):
                return False
            raise
        return True

    def write_done(self, key: str, body: bytes) -> None:
        """Write the ``_DONE`` completion marker (plain put; no conditions)."""
        self.put_object(key, body, storage_class="STANDARD", tags={"purpose": "done"})

    def done_exists(self, key: str) -> bool:
        """True when ``_DONE`` exists (a 404 is the normal "no" answer)."""
        try:
            self.client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _http_status(exc) == 404:
                return False
            raise
        return True

    # -- trees -----------------------------------------------------------

    def upload_tree(
        self,
        local_dir: PathLike,
        prefix: str,
        *,
        storage_class: str,
        tags: Mapping[str, str],
    ) -> dict[str, int]:
        """Upload every file under ``local_dir`` to ``prefix/<relative path>``.

        Returns ``{key: size_bytes}``. The traversal order is deterministic
        (sorted paths), so two uploads of the same tree are comparable.
        """
        root = Path(local_dir)
        results: dict[str, int] = {}
        paths: list[Path] = [p for p in root.rglob("*") if p.is_file()]
        for path in sorted(paths):
            relative = path.relative_to(root).as_posix()
            key = f"{prefix}{relative}"
            self.put_file(key, path, storage_class=storage_class, tags=tags)
            results[key] = path.stat().st_size
        return results
