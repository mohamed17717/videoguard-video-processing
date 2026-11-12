"""Golden-vector tests for the Axinom CENC port (Go cenc.go + cenc_to_shaka.go).

All literals below were generated ONCE with an independent computation
(stdlib hashlib + pycryptodome directly, mirroring the Go byte layout), then
pasted here. They pin:

* the exact request JSON bytes (Go ``json.MarshalIndent(..., "", "  ")``),
* the SHA-1 of those bytes,
* the 32-byte padded digest (20-byte digest + 0x0C x 12),
* the AES-256-CBC ciphertext as base64,
* the compact JSON envelope POSTed to Axinom,
* the combined PSSH v0 box hex,
* the resulting key id / key / IV hex values and SKD URI.

Fixed test inputs (NOT real secrets; all-zero-derived synthetic values are
avoided so a truncation bug cannot cancel out):

* content_id     = 11111111-2222-3333-4444-555555555555
* signing key    = 0123456789abcdef... (32 bytes)
* signing IV     = 0123456789abcdef0123456789abcdef
* provider       = vg-test-provider
* scheme         = CBCS (the contract fix; the old Go code sent CENC)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging

import pytest

from vg_pipeline.cenc import build_combined_pssh, build_pssh_box, request_cenc_keys
from vg_pipeline.contracts import AxinomSecrets, CencKeys
from vg_pipeline.errors import PipelineError

from conftest import capture_records

CONTENT_ID = "11111111-2222-3333-4444-555555555555"
SIGNING_KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SIGNING_IV_HEX = "0123456789abcdef0123456789abcdef"
PROVIDER = "vg-test-provider"
ENDPOINT = "https://cenc.example.invalid/key"

# ---- golden literals (pasted from the independent generator run) ----

REQUEST_JSON = """{
  "content_id": "MTExMTExMTEtMjIyMi0zMzMzLTQ0NDQtNTU1NTU1NTU1NTU1",
  "drm_types": [
    "WIDEVINE",
    "PLAYREADY",
    "FAIRPLAY"
  ],
  "tracks": [
    {
      "type": "HD"
    }
  ],
  "protection_scheme": "CBCS"
}"""

SHA1_HEX = "1db4defd7e353be882d5a34079d212cab95b3a7a"
PADDED_DIGEST_HEX = "1db4defd7e353be882d5a34079d212cab95b3a7a0c0c0c0c0c0c0c0c0c0c0c0c"
CIPHERTEXT_B64 = "I+7HUERYZC2olPWTlDt+jtZaSz+Q+HSMhD0j0cABNSQ="

# The envelopes are assembled from the pinned literals above with Go's exact
# serialisation rules (compact JSON for the POST envelope, base64 payloads),
# so no hand-copied long string can drift from them.
ENVELOPE_JSON = json.dumps(
    {
        "request": base64.b64encode(REQUEST_JSON.encode("utf-8")).decode("ascii"),
        "signature": CIPHERTEXT_B64,
        "signer": PROVIDER,
    },
    separators=(",", ":"),
)

RESPONSE_TEXT = (
    '{"status":"OK","content_id":"MTExMTExMTEtMjIyMi0zMzMzLTQ0NDQtNTU1NTU1NTU1NTU1",'
    '"drm":[{"type":"WIDEVINE","system_id":"edef8ba979d64acea3c827dcd51d21ed"},'
    '{"type":"PLAYREADY","system_id":"9a04f07998404286ab92e65be0885f95"},'
    '{"type":"FAIRPLAY","system_id":"0102030405060708090a0b0c0d0e0f10"}],'
    '"tracks":[{"pssh":[{"drm_type":"WIDEVINE","data":"d3YtcGF5bG9hZC1nb2xkZW4tMDE="},'
    '{"drm_type":"PLAYREADY","data":"cHItcGF5bG9hZC1nb2xkZW4tMDE="},'
    '{"drm_type":"FAIRPLAY","data":""}],'
    '"key_id":"AAAAAAAAAAAAAAAAAAAAAA==","key":"AQIDBAUGBwgJCgsMDQ4PEA==",'
    '"iv":"AgMEBQYHCAkKCwwNDg8QEQ==",'
    '"skd_uri":"skd://vg-test-provider/11111111-2222-3333-4444-555555555555",'
    '"checksum":"Zm9vYmFy"}]}'
)
REPLY_ENVELOPE = json.dumps(
    {"response": base64.b64encode(RESPONSE_TEXT.encode("utf-8")).decode("ascii")},
    separators=(",", ":"),
)

WV_BOX_HEX = "000000347073736800000000edef8ba979d64acea3c827dcd51d21ed0000001477762d7061796c6f61642d676f6c64656e2d3031"
PR_BOX_HEX = "0000003470737368000000009a04f07998404286ab92e65be0885f950000001470722d7061796c6f61642d676f6c64656e2d3031"
PSSH_COMBINED_HEX = WV_BOX_HEX + PR_BOX_HEX

KEY_ID_HEX = "00000000000000000000000000000000"
KEY_HEX = "0102030405060708090a0b0c0d0e0f10"
IV_HEX = "02030405060708090a0b0c0d0e0f1011"
SKD_URI = "skd://vg-test-provider/11111111-2222-3333-4444-555555555555"


def secrets() -> AxinomSecrets:
    return AxinomSecrets.model_validate(
        {
            "signing_key_hex": SIGNING_KEY_HEX,
            "signing_iv_hex": SIGNING_IV_HEX,
            "endpoint": ENDPOINT,
            "provider_name": PROVIDER,
        }
    )


def fake_http_post(status: int = 200, body: bytes | None = None, capture: list | None = None):
    """Build an injectable http_post that records (url, body) and replies."""
    if body is None:
        body = REPLY_ENVELOPE.encode("utf-8")

    def post(url: str, payload: bytes) -> tuple[int, bytes]:
        if capture is not None:
            capture.append((url, payload))
        return status, body

    return post


class TestGoldenVectors:
    def test_request_json_and_envelope_and_keys(self) -> None:
        captured: list[tuple[str, bytes]] = []
        keys = request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(capture=captured))

        # POST target and body bytes
        assert len(captured) == 1
        url, envelope_bytes = captured[0]
        assert url == ENDPOINT
        assert envelope_bytes.decode("utf-8") == ENVELOPE_JSON

        # The request JSON carried inside the envelope matches the golden literal
        envelope = json.loads(envelope_bytes)
        request_text = base64.b64decode(envelope["request"]).decode("utf-8")
        assert request_text == REQUEST_JSON
        assert '"protection_scheme": "CBCS"' in request_text
        assert '"protection_scheme": "CENC"' not in request_text

        # Envelope structure: request / signature / signer, compact JSON
        assert envelope["signature"] == CIPHERTEXT_B64
        assert envelope["signer"] == PROVIDER
        assert list(envelope.keys()) == ["request", "signature", "signer"]

        # Independent SHA-1 recomputation over the golden request bytes
        assert hashlib.sha1(request_text.encode("utf-8")).hexdigest() == SHA1_HEX

        # Padded digest: 20-byte digest + twelve 0x0C bytes (32 total)
        padded = bytes.fromhex(PADDED_DIGEST_HEX)
        assert len(padded) == 32
        assert hashlib.sha1(request_text.encode("utf-8")).digest() + b"\x0c" * 12 == padded
        assert padded[20:] == b"\x0c" * 12

        # Cross-check the ciphertext literal with an inline pycryptodome run
        # over the SAME padded digest and the SAME hex key/IV.
        from Crypto.Cipher import AES

        recomputed = AES.new(
            bytes.fromhex(SIGNING_KEY_HEX), AES.MODE_CBC, bytes.fromhex(SIGNING_IV_HEX)
        ).encrypt(padded)
        assert base64.b64encode(recomputed).decode("ascii") == CIPHERTEXT_B64

        # Returned keys
        assert keys == CencKeys(
            key_id_hex=KEY_ID_HEX,
            key_hex=KEY_HEX,
            iv_hex=IV_HEX,
            pssh_hex=PSSH_COMBINED_HEX,
            skd_uri=SKD_URI,
        )

    def test_combined_pssh_boxes_from_response(self) -> None:
        assert build_combined_pssh(RESPONSE_TEXT.encode("utf-8")) == PSSH_COMBINED_HEX

    def test_single_pssh_box_layout(self) -> None:
        hex_out = build_pssh_box("edef8ba979d64acea3c827dcd51d21ed", "d3YtcGF5bG9hZC1nb2xkZW4tMDE=")
        data = bytes.fromhex(hex_out)
        # size field counts the whole box: 4+4+4+16+4+20 = 52
        assert int.from_bytes(data[0:4], "big") == 52
        assert data[4:8] == b"pssh"
        assert data[8:12] == b"\x00\x00\x00\x00"  # version 0 + flags 0
        assert data[12:28] == bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
        assert int.from_bytes(data[28:32], "big") == 20  # data_size
        assert data[32:] == b"wv-payload-golden-01"
        assert hex_out == WV_BOX_HEX


class TestFailures:
    def test_non_200_status_raises(self) -> None:
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(status=403))
        assert excinfo.value.code == "CENC_FAILED"
        assert "403" in excinfo.value.message

    def test_post_exception_raises(self) -> None:
        def post(url: str, payload: bytes) -> tuple[int, bytes]:
            raise OSError("connection refused")

        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=post)
        assert excinfo.value.code == "CENC_FAILED"

    def test_bad_status_raises(self) -> None:
        response = RESPONSE_TEXT.replace('"status":"OK"', '"status":"ERROR"')
        body = json.dumps({"response": base64.b64encode(response.encode()).decode()}).encode()
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(body=body))
        assert excinfo.value.code == "CENC_FAILED"
        assert "not OK" in excinfo.value.message

    def test_two_tracks_raise(self) -> None:
        response = json.loads(RESPONSE_TEXT)
        track = response["tracks"][0]
        response["tracks"].append(json.loads(json.dumps(track)))
        body = json.dumps({"response": base64.b64encode(json.dumps(response).encode()).decode()}).encode()
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(body=body))
        assert excinfo.value.code == "CENC_FAILED"
        assert "exactly 1 track" in excinfo.value.message

    def test_zero_tracks_raise(self) -> None:
        response = json.loads(RESPONSE_TEXT)
        response["tracks"] = []
        body = json.dumps({"response": base64.b64encode(json.dumps(response).encode()).decode()}).encode()
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(body=body))
        assert excinfo.value.code == "CENC_FAILED"

    def test_garbage_envelope_raises(self) -> None:
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(body=b"not json"))
        assert excinfo.value.code == "CENC_FAILED"

    def test_short_signing_key_raises(self) -> None:
        bad = secrets().model_copy(update={"signing_key_hex": "00" * 16})
        with pytest.raises(PipelineError) as excinfo:
            request_cenc_keys(CONTENT_ID, bad, http_post=fake_http_post())
        assert "32 bytes" in excinfo.value.message

    def test_combined_pssh_missing_system_id_raises(self) -> None:
        response = json.loads(RESPONSE_TEXT)
        response["drm"] = [d for d in response["drm"] if d["type"] != "PLAYREADY"]
        with pytest.raises(PipelineError) as excinfo:
            build_combined_pssh(json.dumps(response).encode())
        assert excinfo.value.code == "CENC_FAILED"
        assert "PLAYREADY" in excinfo.value.message

    def test_combined_pssh_all_empty_raises(self) -> None:
        response = json.loads(RESPONSE_TEXT)
        for entry in response["tracks"][0]["pssh"]:
            entry["data"] = ""
        with pytest.raises(PipelineError) as excinfo:
            build_combined_pssh(json.dumps(response).encode())
        assert excinfo.value.code == "CENC_FAILED"

    def test_combined_pssh_no_tracks_raises(self) -> None:
        response = json.loads(RESPONSE_TEXT)
        response["tracks"] = []
        with pytest.raises(PipelineError) as excinfo:
            build_combined_pssh(json.dumps(response).encode())
        assert excinfo.value.code == "CENC_FAILED"

    def test_bad_system_id_hex_raises(self) -> None:
        with pytest.raises(PipelineError) as excinfo:
            build_pssh_box("not-hex", "AAAA")
        assert excinfo.value.code == "CENC_FAILED"

    def test_wrong_system_id_length_raises(self) -> None:
        with pytest.raises(PipelineError) as excinfo:
            build_pssh_box("aabb", "AAAA")
        assert "16 bytes" in excinfo.value.message


class TestNoSecretsInLogs:
    SECRET_VALUES = (
        SIGNING_KEY_HEX,
        SIGNING_IV_HEX,
        KEY_HEX,
        IV_HEX,
        KEY_ID_HEX,
        CIPHERTEXT_B64,
        # base64 of the request bytes (carries the signing payload context)
        "ewogICJjb250ZW50X2lkIjogIk1URXhNVEV4TVRFdE1qSXlNaTB6TXpNekxUUTBORFF0TlRVMU5UVTFOVFUxTlRVMSIs",
    )

    def test_no_key_or_iv_in_any_log_line(self) -> None:
        captured_http: list[tuple[str, bytes]] = []
        with capture_records(logging.DEBUG) as records:
            request_cenc_keys(CONTENT_ID, secrets(), http_post=fake_http_post(capture=captured_http))
        rendered = "\n".join(
            f"{r.levelname} {r.name} {r.getMessage()} {getattr(r, 'endpoint', '')} "
            f"{getattr(r, 'provider', '')} {getattr(r, 'pssh_boxes', '')}"
            for r in records
        )
        assert len(records) >= 2
        for secret in self.SECRET_VALUES:
            assert secret not in rendered, f"secret leaked into logs: {secret[:16]}..."
        # The response body (contains key/IV) and request bytes never appear.
        assert RESPONSE_TEXT not in rendered
        assert REQUEST_JSON not in rendered
