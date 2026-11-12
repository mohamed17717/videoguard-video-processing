"""Tests for the ClamAV wrapper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vg_pipeline.clamav import build_scan_command, scan
from vg_pipeline.errors import PipelineError


def runner(returncode: int, stdout: str = "", stderr: str = ""):
    def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run


class TestBuildScanCommand:
    def test_flags_and_path(self) -> None:
        cmd = build_scan_command(Path("/work/input/source.mp4"))
        assert cmd[0] == "clamscan"
        assert "--no-summary" in cmd
        assert "--infected" in cmd
        assert cmd[-1] == "/work/input/source.mp4"

    def test_custom_binary(self) -> None:
        cmd = build_scan_command(Path("/x.mp4"), clamscan_bin="/usr/bin/clamscan")
        assert cmd[0] == "/usr/bin/clamscan"


class TestScan:
    def test_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "source.mp4"
        path.write_bytes(b"ok")
        result = scan(path, run=runner(0, stdout="", stderr=""))
        assert result.clean is True
        assert result.output == ""

    def test_infected(self, tmp_path: Path) -> None:
        path = tmp_path / "source.mp4"
        path.write_bytes(b"bad")
        result = scan(
            path,
            run=runner(
                1,
                stdout=f"{path}: Win.Test.EICAR_HDB-1 FOUND",
                stderr="",
            ),
        )
        assert result.clean is False
        assert "FOUND" in result.output

    def test_scan_error_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "source.mp4"
        with pytest.raises(PipelineError) as excinfo:
            scan(path, run=runner(2, stdout="", stderr="LibClamAV Error: database missing"))
        assert excinfo.value.code == "CLAMAV"
        assert "exit 2" in excinfo.value.message

    def test_missing_binary_raises(self, tmp_path: Path) -> None:
        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            raise FileNotFoundError("clamscan not installed")

        with pytest.raises(PipelineError) as excinfo:
            scan(tmp_path / "source.mp4", run=run)
        assert excinfo.value.code == "CLAMAV"
        assert "cannot run clamscan" in excinfo.value.message

    def test_command_passed_to_runner(self, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def run(cmd: list[str], capture_output: bool = True, text: bool = True) -> subprocess.CompletedProcess:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        scan(tmp_path / "source.mp4", run=run)
        assert seen == [["clamscan", "--no-summary", "--infected", str(tmp_path / "source.mp4")]]
