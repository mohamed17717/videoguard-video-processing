"""Tests for JSON logging and secret redaction."""

from __future__ import annotations

import json
import logging

import pytest

from vg_pipeline.logging import configure_logging, get_logger, redact


class TestRedact:
    def test_scrubs_key_iv_secret_values(self) -> None:
        data = {
            "key": "aaa",
            "key_hex": "0102",
            "key_id": "0000",
            "iv": "abcd",
            "iv_hex": "abcd",
            "signing_key_hex": "0123",
            "signing_iv_hex": "4567",
            "webhook_secret": "s3cret",
            "signature": "sig",
            "token": "tok",
            "job_id": "job",
            "width": 1280,
        }
        cleaned = redact(data)
        for secret_key in (
            "key",
            "key_hex",
            "key_id",
            "iv",
            "iv_hex",
            "signing_key_hex",
            "signing_iv_hex",
            "webhook_secret",
            "signature",
            "token",
        ):
            assert cleaned[secret_key] == "[REDACTED]"
        assert cleaned["job_id"] == "job"
        assert cleaned["width"] == 1280

    def test_nested_structures(self) -> None:
        data = {"outer": {"inner_key": "v", "list": [{"secret": "x"}, "ok"]}, "plain": 1}
        cleaned = redact(data)
        assert cleaned["outer"]["inner_key"] == "[REDACTED]"
        assert cleaned["outer"]["list"][0]["secret"] == "[REDACTED]"
        assert cleaned["outer"]["list"][1] == "ok"
        assert cleaned["plain"] == 1

    def test_original_not_mutated(self) -> None:
        data = {"key": "aaa", "plain": 1}
        redact(data)
        assert data["key"] == "aaa"

    def test_non_dict_passthrough(self) -> None:
        assert redact("hello") == "hello"
        assert redact(42) == 42
        assert redact(None) is None


class TestJsonLogging:
    def test_json_lines_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("DEBUG")
        logger = get_logger("test")
        logger.info("hello", extra={"job_id": "j", "key_hex": "aa"})
        err = capsys.readouterr().err
        lines = [line for line in err.strip().splitlines() if line]
        assert lines, "expected at least one JSON log line on stderr"
        entry = json.loads(lines[-1])
        assert entry["msg"] == "hello"
        assert entry["level"] == "INFO"
        assert entry["logger"].startswith("vg_pipeline")
        assert entry["job_id"] == "j"
        # structured extras are redacted before output
        assert entry["key_hex"] == "[REDACTED]"

    def test_configure_is_idempotent(self) -> None:
        root = logging.getLogger()
        configure_logging("INFO")
        count_before = sum(
            1 for handler in root.handlers if getattr(handler, "vg_pipeline_json_handler", False)
        )
        configure_logging("INFO")
        count_after = sum(
            1 for handler in root.handlers if getattr(handler, "vg_pipeline_json_handler", False)
        )
        assert count_before == 1
        assert count_after == 1

    def test_bad_level_raises(self) -> None:
        with pytest.raises(KeyError):
            configure_logging("NOT_A_LEVEL")
