"""Shared fixtures for the pipeline tests."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from vg_pipeline.contracts import PipelineConfig, load_pipeline_config

# Repository root (this file is tests/pipeline/conftest.py in the standalone
# video-processing repo).
VIDEO_PROCESSING_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_CONFIG_PATH = VIDEO_PROCESSING_ROOT / "configs" / "pipeline.yaml"


@pytest.fixture(scope="session")
def pipeline_cfg() -> PipelineConfig:
    """The shipped configs/pipeline.yaml."""
    return load_pipeline_config(PIPELINE_CONFIG_PATH)


@contextmanager
def capture_records(level: int = logging.DEBUG) -> Iterator[list[logging.LogRecord]]:
    """Capture every log record emitted while the block runs."""
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = ListHandler(level=level)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
