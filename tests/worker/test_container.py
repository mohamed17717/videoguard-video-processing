"""Container argv contracts and generator output verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vg_pipeline.errors import PipelineError
from vg_worker.container import (
    GeneratorResult,
    build_generator_argv,
    build_validator_argv,
    run_generator,
    run_validator,
)

from conftest import completes, generator_handler, parses_mounts

GEN_IMAGE = "123.dkr.ecr.us-east-1.amazonaws.com/forensic-wm-generator:hevc-v1-sha"
VAL_IMAGE = "123.dkr.ecr.us-east-1.amazonaws.com/vg-validator:pinned"


def test_generator_argv_hardening() -> None:
    argv = build_generator_argv(GEN_IMAGE, "/work-host/input", "/work-host/out", "/run/vg-secrets/watermark", "cuda")
    text = " ".join(argv)
    # container argv must come from the configured docker binary
    assert argv[0] == "docker" and argv[1:3] == ["run", "--rm"]
    for flag in ("--network=none", "--read-only"):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "--gpus" in argv and argv[argv.index("--gpus") + 1] == "all"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=512m"
    assert "-e" in argv and "WATERMARK_SECRET_FILE=/run/secrets/watermark" in argv
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility" in argv
    assert argv.index("-e") < argv.index(GEN_IMAGE), "image is a positional tail"
    assert argv.index(GEN_IMAGE) == len(argv) - 7
    assert argv[argv.index(GEN_IMAGE) + 1:] == ["--input", "/work/input/source.mp4",
                                                "--output-dir", "/work/output/generated", "--backend", "cuda"]
    mounts = parses_mounts(argv)
    assert mounts["/work/input"][1] == "ro"
    assert mounts["/work/output"][1] == "rw"
    assert mounts["/run/secrets/watermark"][1] == "ro"
    assert mounts["/work/input"][0] == "/work-host/input"
    assert mounts["/work/output"][0] == "/work-host/out"
    assert mounts["/run/secrets/watermark"][0] == "/run/vg-secrets/watermark"


def test_validator_argv_no_gpu() -> None:
    argv = build_validator_argv(VAL_IMAGE, "/work-host/input", "/usr/local/bin/ffprobe", "clamscan")
    assert "--gpus" not in argv
    for flag in ("--network=none", "--read-only", "--rm"):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=512m"
    tail = argv[argv.index(VAL_IMAGE) + 1:]
    assert tail == ["--input", "/work/input/source.mp4",
                    "--ffprobe-bin", "/usr/local/bin/ffprobe", "--clamscan-bin", "clamscan"]
    mounts = parses_mounts(argv)
    assert mounts["/work/input"][1] == "ro"


def test_empty_image_rejected() -> None:
    with pytest.raises(ValueError):
        build_generator_argv("", "a", "b", "c")


def test_run_generator_success(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    out_dir = tmp_path / "out"
    input_dir.mkdir()
    (input_dir / "source.mp4").write_bytes(b"src")
    result: GeneratorResult = run_generator(
        GEN_IMAGE, input_dir, out_dir, "/run/vg-secrets/watermark", "cuda", generator_handler()
    )
    assert result.completion["status"] == "ok"
    assert result.variant_a.is_file()
    assert result.variant_a.stat().st_size > 0
    assert result.variant_b.name == "variant_b.mp4"
    assert result.config_json.is_file()
    assert result.generation_report.is_file()


def _unhappy_runner(code: int, stdout: str = "") -> object:
    def runner(argv: list[str]) -> object:
        return completes(stdout=stdout, stderr="boom", code=code)

    return runner


def test_generator_exit_two_is_deterministic(tmp_path: Path) -> None:
    with pytest.raises(PipelineError) as excinfo:
        run_generator(GEN_IMAGE, tmp_path, tmp_path / "o", "/sec", "cuda", _unhappy_runner(2))
    assert excinfo.value.code == "GENERATOR_EXIT_2"


@pytest.mark.parametrize("code", [130, 143])
def test_generator_interrupt_propagates_as_transient(tmp_path: Path, code: int) -> None:
    from vg_worker.container import ContainerError

    with pytest.raises(ContainerError):
        run_generator(GEN_IMAGE, tmp_path, tmp_path / "o", "/sec", "cuda", _unhappy_runner(code))


def test_generator_other_exit_is_transient(tmp_path: Path) -> None:
    from vg_worker.container import ContainerError

    with pytest.raises(ContainerError):
        run_generator(GEN_IMAGE, tmp_path, tmp_path / "o", "/sec", "cuda", _unhappy_runner(137))


def test_generator_without_completion_json_fails(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="no completion JSON"):
        run_generator(GEN_IMAGE, tmp_path, tmp_path / "o", "/sec", "cuda", _unhappy_runner(0, stdout="plain logs"))


def test_generator_bad_status_fails(tmp_path: Path) -> None:
    doc = json.dumps({"status": "error"})
    with pytest.raises(PipelineError, match="status"):
        run_generator(GEN_IMAGE, tmp_path, tmp_path / "o", "/sec", "cuda", _unhappy_runner(0, stdout=doc))


def test_generator_missing_output_fails(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    (out_dir / "generated").mkdir(parents=True)
    (out_dir / "generated" / "variant_a.mp4").write_bytes(b"A" * 8)
    (out_dir / "generated" / "variant_b.mp4").write_bytes(b"B" * 8)
    (out_dir / "generated" / "config.json").write_bytes(b"{}")
    # generation_report.json MISSING -> contract breach
    doc = json.dumps({"status": "ok"})

    def runner(argv: list[str]) -> object:
        return completes(stdout=doc)

    with pytest.raises(PipelineError) as excinfo:
        run_generator(GEN_IMAGE, tmp_path / "in", out_dir, "/sec", "cuda", runner)
    assert excinfo.value.code == "GENERATOR_EXIT_2"


def test_generator_empty_output_file_fails(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    (out_dir / "generated").mkdir(parents=True)
    for name, payload in (
        ("variant_a.mp4", b"A" * 8),
        ("variant_b.mp4", b"B" * 8),
        ("config.json", b"{}"),
        ("generation_report.json", b""),
    ):
        (out_dir / "generated" / name).write_bytes(payload)
    doc = json.dumps({"status": "ok"})

    def runner(argv: list[str]) -> object:
        return completes(stdout=doc)

    with pytest.raises(PipelineError, match="generation_report"):
        run_generator(GEN_IMAGE, tmp_path / "in", out_dir, "/sec", "cuda", runner)


VERDICT = json.dumps(
    {"ffprobe": {"streams": [], "format": {}}, "ffprobe_exit": 0, "clamscan_exit": 0, "clam_output": "", "error": ""}
)


def test_validator_verdict_parse() -> None:
    def runner(argv: list[str]) -> object:
        # the validator container prints human logs BEFORE the JSON verdict
        return completes(stdout="starting\nprobe ok\n" + VERDICT)

    verdict = run_validator(VAL_IMAGE, "/host/in", "ffprobe", "clamscan", runner)
    assert verdict.ffprobe_exit == 0
    assert verdict.clamscan_exit == 0
    assert verdict.ffprobe == {"streams": [], "format": {}}


def test_validator_docker_failure_is_transient() -> None:
    from vg_worker.container import ContainerError

    with pytest.raises(ContainerError):
        run_validator(VAL_IMAGE, "/host/in", "ffprobe", "clamscan", _unhappy_runner(125))


def test_validator_without_verdict_json_is_transient() -> None:
    from vg_worker.container import ContainerError

    with pytest.raises(ContainerError, match="no verdict JSON"):
        run_validator(VAL_IMAGE, "/host/in", "ffprobe", "clamscan", _unhappy_runner(0, stdout="no json here"))
