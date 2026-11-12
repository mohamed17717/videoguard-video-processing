"""Structured JSON logging on stderr plus a secret-redaction helper.

Rules (CONTRACT.md section 10):

* every log line is one JSON object on stderr, so a log collector can parse it;
* values that could be a secret (key, IV, signing key, token, ...) must never
  reach a log line. :func:`redact` scrubs them by key name, and every module in
  this package logs only through :func:`get_logger`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Key-name fragments that mark a value as secret. Matching is
# case-insensitive and by substring, so "key_id" is scrubbed too. That is
# deliberate: logs never need the key id, and scrubbing it removes a class of
# mistakes.
_SECRET_FRAGMENTS: tuple[str, ...] = (
    "key",
    "iv",
    "secret",
    "signature",
    "token",
    "password",
    "credential",
)
_REDACTED = "[REDACTED]"
_HANDLER_MARKER = "vg_pipeline_json_handler"


def _is_secret_key(key: str) -> bool:
    """True when a dict/field name looks like it carries a secret."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_FRAGMENTS)


def redact(data: Any) -> Any:
    """Return a copy of ``data`` with secret-looking values scrubbed.

    Works on nested dicts and lists. A dict entry whose key contains any
    secret fragment (for example ``key``, ``iv``, ``signing_key_hex``) keeps
    the key but replaces the value with ``[REDACTED]``. Other values are
    returned unchanged. Use it before logging any dict that was near a secret.
    """
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, value in data.items():
            key = str(raw_key)
            cleaned[key] = _REDACTED if _is_secret_key(key) else redact(value)
        return cleaned
    if isinstance(data, (list, tuple)):
        cleaned_items = [redact(item) for item in data]
        return cleaned_items if isinstance(data, list) else tuple(cleaned_items)
    return data


# Standard attributes every logging.LogRecord carries. Extras supplied by the
# caller are merged into record.__dict__ as well, so anything outside this set
# is a caller-supplied structured field.
_STD_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
) | {"message", "asctime"}


class _JsonFormatter(logging.Formatter):
    """Format one log record as one JSON line."""

    def format(self, record: logging.Record) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Copy every caller-supplied attribute into the entry. Field names
        # that look secret-bearing are scrubbed by name; other values are
        # redacted recursively, so callers can attach structured fields such
        # as ``job_id``. Standard bookkeeping fields are skipped.
        for attr, value in record.__dict__.items():
            if attr not in _STD_RECORD_ATTRS and not attr.startswith("_"):
                entry[attr] = _REDACTED if _is_secret_key(attr) else redact(value)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Route all logging of this process to stderr as JSON lines.

    Idempotent: calling it twice keeps one handler. ``level`` is a standard
    name such as ``"INFO"`` or ``"DEBUG"``; an unknown name is a real error,
    because a silent fallback could hide job failures.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(logging.getLevelNamesMapping()[level.upper()])


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under ``vg_pipeline``."""
    return logging.getLogger(f"vg_pipeline.{name}")
