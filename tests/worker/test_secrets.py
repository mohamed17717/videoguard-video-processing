"""Secrets Manager parsing and the watermark tmpfs file."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from vg_worker import secrets as secrets_mod
from vg_worker.secrets import fetch_secrets, watermark_secret_file, write_watermark_tmpfs

from conftest import axinom_secret_content, fake_secrets, webhook_secret_content, watermark_secret_content

ARNS = {
    "webhook": "arn:aws:secretsmanager:wh",
    "axinom": "arn:aws:secretsmanager:ax",
    "watermark": "arn:aws:secretsmanager:wm",
}


def test_fetch_the_three_shapes():
    fetched = fetch_secrets(fake_secrets(ARNS), ARNS)
    assert fetched["webhook"]["secret"] == webhook_secret_content()["secret"]
    assert fetched["watermark"]["secret"] == watermark_secret_content()["secret"]
    axinom = fetched["axinom"]
    for field in ("signing_key_hex", "signing_iv_hex", "endpoint", "provider_name", "scheme"):
        assert field in axinom
    assert fetched["axinom"]["scheme"] == "cbcs"


def test_version_ids_available():
    fetched = fetch_secrets(fake_secrets(ARNS), ARNS)
    assert fetched.version_of("watermark") == "wmv7"
    assert fetched.version_of("webhook") == "wv1"


def test_unknown_name_rejected():
    with pytest.raises(ValueError, match="unknown logical secret name"):
        fetch_secrets(fake_secrets(ARNS), {**ARNS, "banana": "arn:x"})


def test_missing_arn_rejected():
    partial = {k: v for k, v in ARNS.items() if k != "watermark"}
    with pytest.raises(ValueError, match="watermark"):
        fetch_secrets(fake_secrets(ARNS), partial)


def test_invalid_json_rejected_without_leaking_content():
    class Broken:
        def get_secret_value(self, *, SecretId: str):
            return {"SecretString": "{not-json", "VersionId": "1"}

    with pytest.raises(ValueError, match="not valid JSON"):
        fetch_secrets(Broken(), ARNS)


def test_wrong_shape_rejected():
    class BadShape:
        def get_secret_value(self, *, SecretId: str):
            if SecretId == ARNS["watermark"]:
                return {"SecretString": '{"banana": 1}', "VersionId": "1"}
            return fake_secrets(ARNS).get_secret_value(SecretId=SecretId)

    with pytest.raises(ValueError, match="missing required"):
        fetch_secrets(BadShape(), ARNS)


def test_client_error_mentions_name_and_arn_only():
    class NotAuthorized:
        def get_secret_value(self, *, SecretId: str):
            raise RuntimeError("AccessDenied with huge secret blob inside")

    with pytest.raises(ValueError, match=r"cannot fetch secret"):
        fetch_secrets(NotAuthorized(), ARNS)


def test_write_watermark_tmpfs_writes_0400_and_cleanup_unlinks(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "vg-secrets"
    with watermark_secret_file("watermark-secret-value", directory=secrets_dir) as path:
        assert path.name == "watermark"
        assert path.read_text() == "watermark-secret-value"
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.exists()
    assert not path.exists()  # unlinked after the generator container exits


def test_cleanup_runs_even_on_error(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "vg-secrets"
    with pytest.raises(RuntimeError):
        with watermark_secret_file("secret", directory=secrets_dir) as path:
            assert path.exists()
            raise RuntimeError("generator exploded")
    assert not path.exists()


def test_owner_chown_called_when_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(secrets_mod, "_geteuid", lambda: 0)
    monkeypatch.setattr(
        secrets_mod.os,
        "chown",
        lambda path, uid, gid: calls.append((path, uid, gid)),
    )
    path = tmp_path / "vg-secrets" / "watermark"
    write_watermark_tmpfs("s", path)
    assert calls == [(path, 10001, 10001)]
    path.unlink()


def test_empty_secret_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        write_watermark_tmpfs("", tmp_path / "vg-secrets" / "watermark")


def test_axinom_content_shape_documented():
    """Guard the CONTRACT.md section 5 shape so refactors cannot drift."""
    content = axinom_secret_content()
    assert set(content) == {"signing_key_hex", "signing_iv_hex", "endpoint", "provider_name", "scheme"}
