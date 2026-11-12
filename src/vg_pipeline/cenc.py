"""Axinom CENC key request, ported byte-for-byte from ``internal/transcode``.

Ports Go ``cenc.go`` (request building, SHA-1 signing, AES-256-CBC envelope)
and ``cenc_to_shaka.go`` (response parsing, combined PSSH box), with the
contract-mandated fix: ``protection_scheme`` is ``"CBCS"`` here, not the
``"CENC"`` the old Go code hard-coded (CONTRACT.md section 6).

Wire format, exactly as the Go code produces it:

1. Request object marshalled with two-space indentation, keys in this order:
   ``content_id`` (base64 of the content id), ``drm_types``, ``tracks``,
   ``protection_scheme``.
2. SHA-1 over those exact request bytes.
3. The 20-byte digest padded to 32 bytes with ``0x0C`` (PKCS#7-style: 12
   bytes of value 12), then AES-256-CBC encrypted with the hex signing key
   and hex IV. Base64 of the ciphertext is ``signature``.
4. Compact-JSON envelope ``{"request": ..., "signature": ..., "signer": ...}``
   POSTed to the endpoint with ``Content-Type: application/json``.
5. The reply envelope's ``response`` field is base64; the decoded bytes are
   the Axinom JSON with ``status``, ``drm`` and ``tracks``.

SECURITY: the request bytes carry the signing key, the response bytes carry
the content key and IV. Nothing in this module logs any of those bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from collections.abc import Callable

from .contracts import AxinomSecrets, CencKeys
from .errors import PipelineError
from .logging import get_logger

log = get_logger("cenc")

# Fixed request fields, in Go struct order.
_DRM_TYPES = ("WIDEVINE", "PLAYREADY", "FAIRPLAY")
_TRACK = {"type": "HD"}
# Contract fix (section 6): the Go code sent "CENC"; Shaka uses cbcs.
_PROTECTION_SCHEME = "CBCS"

# Signature input layout: SHA-1 digest (20 bytes) + 0x0C padding to 32 bytes.
_DIGEST_LEN = 20
_PADDED_LEN = 32
_PAD_VALUE = _PADDED_LEN - _DIGEST_LEN  # 0x0C

# HTTP callable used for the key request:
# ``HttpPost(url: str, body: bytes) -> tuple[int, bytes]`` returns the HTTP
# status and the response body. Injectable for tests; the default uses
# :func:`urllib.request`.
HttpPost = Callable[[str, bytes], tuple[int, bytes]]


def _default_http_post(url: str, body: bytes) -> tuple[int, bytes]:
    """POST ``body`` as JSON and return ``(status, response_bytes)``."""
    request = urllib.request.Request(  # noqa: S310 - endpoint comes from Secrets Manager
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        return response.status, response.read()


def _b64_to_hex(value: str, what: str) -> str:
    """Decode one base64 field of the Axinom response to lowercase hex."""
    try:
        return base64.b64decode(value).hex()
    except Exception as exc:  # binascii.Error subclasses ValueError
        raise PipelineError("CENC_FAILED", f"cannot decode {what} from Axinom response: {exc}") from exc


def _encode_request(content_id: str) -> bytes:
    """Build the exact request JSON bytes (Go ``json.MarshalIndent(r, "", "  ")``).

    Python ``json.dumps(indent=2)`` with default separators produces the same
    bytes as Go for this payload: both use ``,`` / ``: `` and indent nested
    arrays and objects. Keys are inserted in Go struct order. ``ensure_ascii``
    matches Go's raw-UTF-8 output, and the only variable string (base64 of
    the content id) cannot contain characters Go would HTML-escape.
    """
    request_obj = {
        "content_id": base64.b64encode(content_id.encode("utf-8")).decode("ascii"),
        "drm_types": list(_DRM_TYPES),
        "tracks": [dict(_TRACK)],
        "protection_scheme": _PROTECTION_SCHEME,
    }
    return json.dumps(request_obj, indent=2, ensure_ascii=False).encode("utf-8")


def _sign(request_text: bytes, secrets: AxinomSecrets) -> str:
    """SHA-1 the request, pad to 32 bytes with 0x0C, AES-256-CBC, base64."""
    digest = hashlib.sha1(request_text).digest()
    padded = digest + bytes([_PAD_VALUE]) * (_PADDED_LEN - _DIGEST_LEN)
    # Imported lazily so environments without pycryptodome (for example the
    # network-less validator container, which never requests keys) can still
    # import this module.
    from Crypto.Cipher import AES  # pycryptodome, pinned in requirements-worker.txt

    key = bytes.fromhex(secrets.signing_key_hex)
    iv = bytes.fromhex(secrets.signing_iv_hex)
    if len(key) != 32:
        raise PipelineError("CENC_FAILED", f"signing key must be 32 bytes, got {len(key)}")
    if len(iv) != 16:
        raise PipelineError("CENC_FAILED", f"signing IV must be 16 bytes, got {len(iv)}")
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    return base64.b64encode(ciphertext).decode("ascii")


def _decode_response_body(body: bytes) -> bytes:
    """Peel the outer envelope and base64-decode its ``response`` field."""
    try:
        envelope = json.loads(body)
        decoded = base64.b64decode(envelope["response"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError("CENC_FAILED", f"cannot decode Axinom reply envelope: {exc}") from exc
    if not isinstance(decoded, bytes):
        decoded = bytes(decoded)
    return decoded


def request_cenc_keys(
    content_id: str,
    secrets: AxinomSecrets,
    http_post: HttpPost | None = None,
) -> CencKeys:
    """Request one CENC key set from Axinom for ``content_id``.

    ``http_post`` is injectable for tests; the default uses
    :mod:`urllib.request`. Asserts ``status == "OK"`` and exactly one track;
    anything else raises :class:`PipelineError` with code ``CENC_FAILED``.

    The request and response bytes contain key material. They are never
    logged and never placed in an exception message.
    """
    post = http_post or _default_http_post
    request_text = _encode_request(content_id)
    signature = _sign(request_text, secrets)
    envelope = json.dumps(
        {
            "request": base64.b64encode(request_text).decode("ascii"),
            "signature": signature,
            "signer": secrets.provider_name,
        },
        # Go json.Marshal (compact): no spaces after separators.
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    log.info("axinom key request", extra={"endpoint": secrets.endpoint, "provider": secrets.provider_name})
    try:
        status, body = post(secrets.endpoint, envelope)
    except PipelineError:
        raise
    except Exception as exc:
        # Never include ``exc`` text from a caller-provided callable that saw
        # the envelope; the message names the endpoint only.
        raise PipelineError("CENC_FAILED", f"axinom key request to {secrets.endpoint} failed") from exc
    if status != 200:
        raise PipelineError("CENC_FAILED", f"axinom key request returned http status {status}")
    log.info("axinom key response received", extra={"status": status})

    decoded = _decode_response_body(body)
    try:
        response = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise PipelineError("CENC_FAILED", f"axinom response payload is not JSON: {exc}") from exc
    if response.get("status") != "OK":
        raise PipelineError("CENC_FAILED", f"axinom status not OK: {response.get('status')!r}")
    tracks = response.get("tracks") or []
    if len(tracks) != 1:
        raise PipelineError("CENC_FAILED", f"expected exactly 1 track in axinom response, got {len(tracks)}")
    track = tracks[0]
    pssh_hex = build_combined_pssh(decoded)
    keys = CencKeys(
        key_id_hex=_b64_to_hex(track.get("key_id", ""), "key_id"),
        key_hex=_b64_to_hex(track.get("key", ""), "key"),
        iv_hex=_b64_to_hex(track.get("iv", ""), "iv"),
        pssh_hex=pssh_hex,
        skd_uri=str(track.get("skd_uri", "")),
    )
    log.info("axinom keys parsed", extra={"pssh_boxes": pssh_hex.count("70737368"), "drm_systems": len(response.get("drm") or [])})
    return keys


def build_pssh_box(system_id_hex: str, b64_data: str) -> str:
    """Build ONE PSSH version-0 box as lowercase hex.

    Layout (Go ``buildPSSHBox``): big-endian ``size``, ``'pssh'``, a zero
    32-bit version+flags word, the 16-byte ``system_id``, big-endian
    ``data_size``, then the decoded payload bytes. ``size`` counts the whole
    box: 4 + 4 + 4 + 16 + 4 + len(data).
    """
    try:
        system_id = bytes.fromhex(system_id_hex)
    except ValueError as exc:
        raise PipelineError("CENC_FAILED", f"invalid system_id hex {system_id_hex!r}: {exc}") from exc
    if len(system_id) != 16:
        raise PipelineError("CENC_FAILED", f"system_id must be 16 bytes, got {len(system_id)}")
    try:
        payload = base64.b64decode(b64_data)
    except Exception as exc:
        raise PipelineError("CENC_FAILED", f"invalid base64 pssh data: {exc}") from exc
    box_size = 4 + 4 + 4 + 16 + 4 + len(payload)
    parts = [
        box_size.to_bytes(4, "big"),
        b"pssh",
        (0).to_bytes(4, "big"),
        system_id,
        len(payload).to_bytes(4, "big"),
        payload,
    ]
    return b"".join(parts).hex()


def build_combined_pssh(response_json: bytes) -> str:
    """Concatenate every non-empty PSSH box of the response as one hex string.

    Port of Go ``BuildCombinedPSSHHex``: uses ``tracks[0]``, maps each entry's
    ``drm_type`` (upper-cased) to a ``system_id`` from the ``drm`` list,
    skips entries with empty ``data`` (Axinom returns FairPlay that way), and
    fails when a system id is missing or no box could be built.
    """
    try:
        response = json.loads(response_json)
    except json.JSONDecodeError as exc:
        raise PipelineError("CENC_FAILED", f"cannot parse axinom response: {exc}") from exc
    tracks = response.get("tracks") or []
    if not tracks:
        raise PipelineError("CENC_FAILED", "no tracks in axinom response")
    track = tracks[0]

    system_id_by_type = {
        str(entry.get("type", "")).upper(): str(entry.get("system_id", "")) for entry in (response.get("drm") or [])
    }

    boxes: list[str] = []
    for entry in track.get("pssh") or []:
        drm_type = str(entry.get("drm_type", "")).upper()
        data = str(entry.get("data", ""))
        if data == "":
            # No payload means no PSSH box for that system (FairPlay).
            continue
        system_id = system_id_by_type.get(drm_type)
        if system_id is None:
            raise PipelineError("CENC_FAILED", f"no system_id for drm_type {drm_type}")
        boxes.append(build_pssh_box(system_id, data))
    if not boxes:
        raise PipelineError("CENC_FAILED", "no PSSH boxes could be built (all empty?)")
    # Shaka expects concatenated full boxes without separators.
    return "".join(boxes)
