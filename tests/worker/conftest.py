"""Offline fixtures for the worker tests: no network, no docker, no boto3.

Everything the worker touches at run time is a fake here: SQS, S3, Secrets
Manager, IMDS, the ASG, the docker/ffmpeg/packager subprocesses, the webhook
poster and clocks. The media logic itself (probe parsing, the aligned-timeline
gate, canonical manifests) comes from the real ``vg_pipeline`` library, so the
integration tests cover the REAL wiring end to end.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vg_pipeline.contracts import JobMessage
from vg_worker.config import WorkerConfig
from vg_worker.main import Collaborators

# Repository root (this file is tests/worker/conftest.py in the standalone
# video-processing repo).
VIDEO_PROCESSING_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_CONFIG_PATH = VIDEO_PROCESSING_ROOT / "configs" / "pipeline.yaml"


# ---------------------------------------------------------------------------
# Minimal synthetic MP4 boxes: enough for vg_pipeline.mp4 to canonicalise inits
# and read tfdt decode times from segments (no real Shaka is available here).
# ---------------------------------------------------------------------------

def box(btype: bytes, payload: bytes) -> bytes:
    """One ISO-BMFF box: 4-byte length + type + payload."""
    return (8 + len(payload)).to_bytes(4, "big") + btype + payload


def make_init(creation_time: int = 0, modification_time: int = 0) -> bytes:
    """A tiny init segment with a moov/tkhd carrying wall-clock-like times.

    Two such inits differ by their times only; canonicalisation must make
    them byte-equal.
    """
    tkhd = box(b"tkhd", bytes(4) + creation_time.to_bytes(4, "big") + modification_time.to_bytes(4, "big") + bytes(12))
    moov = box(b"moov", tkhd)
    ftyp = box(b"ftyp", b"isom" + (0x200).to_bytes(2, "big") + b"isomiso2")
    return ftyp + moov


def make_segment(decode_time: int) -> bytes:
    """A tiny media segment carrying one moof/traf/tfdt decode time."""
    tfdt = box(b"tfdt", bytes(4) + decode_time.to_bytes(4, "big"))
    traf = box(b"traf", tfdt)
    moof = box(b"moof", traf)
    return moof


# ---------------------------------------------------------------------------
# Shared test job data
# ---------------------------------------------------------------------------

JOB_ID = "11111111-2222-3333-4444-555555555555"
CONTENT_ID = "33333333-4444-5555-6666-777777777777"
VIDEO_PUBLIC_ID = CONTENT_ID  # the content id MUST equal the video public id
RUN_ID = "aaaa1111-2222-3333-4444-555555555555"
QUEUED_AT = "2026-01-01T00:00:00Z"
SIZE_BYTES = 64
SOURCE_BYTES = bytes(range(64))
INCOMING_KEY = f"incoming/{JOB_ID}/source.mp4"


def sidecar_dict(
    run_epoch: int = 1,
    run_id: str = RUN_ID,
    retention: str = "archive",
    size_bytes: int = SIZE_BYTES,
) -> dict:
    """A complete, valid sidecar document (CONTRACT.md section 1)."""
    return {
        "schema_version": 1,
        "job_id": JOB_ID,
        "run_id": run_id,
        "run_epoch": run_epoch,
        "video_public_id": VIDEO_PUBLIC_ID,
        "tenant_slug": "acme",
        "content_id": CONTENT_ID,
        "content_type": "video/mp4",
        "size_bytes": size_bytes,
        "retention": retention,
        "trace_id": "trace-0000-0001",
        "created_at": QUEUED_AT,
    }


def job_model(run_epoch: int = 1, retention: str = "archive", reason: str = "requeue") -> JobMessage:
    """A canonical JobMessage as the backend would send it."""
    return JobMessage.model_validate({**sidecar_dict(run_epoch=run_epoch, retention=retention), "reason": reason})


def raw_s3_event(key: str, size: int) -> dict:
    """The bucket-notification message shape for `incoming/<job>/source.mp4`."""
    return {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put",
                         "s3": {"bucket": {"name": "vg-media-dev"}, "object": {"key": key, "size": size}}}]}


# ---------------------------------------------------------------------------
# Fake S3 (client-level, boto3-shaped kwargs) with conditional-write semantics
# ---------------------------------------------------------------------------

class FakeS3Error(Exception):
    """botocore-shaped error: ``.response`` carries the HTTP status."""

    def __init__(self, status: int, code: str = "PreconditionFailed") -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeObject:
    def __init__(self, data: bytes, etag: str, storage_class: str, tagging: str) -> None:
        self.data = data
        self.etag = etag
        self.storage_class = storage_class
        self.tagging = tagging


class FakeS3:
    """In-memory S3 client implementing exactly the protocol S3IO uses."""

    def __init__(self) -> None:
        self.objects: dict[str, FakeObject] = {}
        self.events: list[tuple[str, str]] = []  # ordered ("put"|"head"|"tag"|"get", key)

    # -- helpers for tests -------------------------------------------------

    def put(self, key: str, data: bytes) -> str:
        """Direct write (test setup) returning the new ETag."""
        etag = f'"{uuid.uuid4().hex}"'
        self.objects[key] = FakeObject(data, etag, "STANDARD", "")
        return etag

    def has(self, key: str) -> bool:
        return key in self.objects

    # -- S3Client protocol ------------------------------------------------

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        del Bucket
        obj = self.objects.get(Key)
        if obj is None:
            raise FakeS3Error(404, "NoSuchKey")
        self.events.append(("get", Key))
        return {"Body": SimpleNamespace(read=lambda: obj.data), "ETag": obj.etag}

    def put_object(self, **kwargs: Any) -> dict:
        key = kwargs["Key"]
        if_none_match = kwargs.get("IfNoneMatch")
        if_match = kwargs.get("IfMatch")
        existing = self.objects.get(key)
        if if_none_match == "*" and existing is not None:
            raise FakeS3Error(412)
        if if_match is not None and (existing is None or existing.etag != if_match):
            raise FakeS3Error(412)
        etag = f'"{uuid.uuid4().hex}"'
        self.objects[key] = FakeObject(
            data=kwargs["Body"],
            etag=etag,
            storage_class=kwargs.get("StorageClass", "STANDARD"),
            tagging=kwargs.get("Tagging", ""),
        )
        self.events.append(("put", key))
        return {"ETag": etag}

    def upload_fileobj(self, fileobj: Any, bucket: str, key: str, **kwargs: Any) -> None:
        del bucket
        extra = kwargs.get("ExtraArgs") or {}
        self.put_object(Key=key, Body=fileobj.read(), **extra)

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        del Bucket
        obj = self.objects.get(Key)
        if obj is None:
            raise FakeS3Error(404, "NoSuchKey")
        self.events.append(("head", Key))
        return {"ETag": obj.etag, "ContentLength": len(obj.data)}

    def put_object_tagging(self, *, Bucket: str, Key: str, Tagging: dict) -> dict:
        del Bucket
        obj = self.objects.get(Key)
        if obj is None:
            raise FakeS3Error(404, "NoSuchKey")
        obj.tagging = json.dumps(Tagging, sort_keys=True)
        self.events.append(("tag", Key))
        return {}


def preload_job(s3: FakeS3, sidecar: Mapping, source: bytes = SOURCE_BYTES) -> None:
    """Load the sidecar + uploaded object for one job into the fake bucket."""
    s3.put(f"jobs/{JOB_ID}/job.json", dict_to_bytes(sidecar))
    s3.put(INCOMING_KEY, source)


def dict_to_bytes(value: Mapping) -> bytes:
    return json.dumps(dict(value), sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake SQS / Secrets Manager / IMDS / ASG
# ---------------------------------------------------------------------------

class FakeSqs:
    def __init__(self) -> None:
        self.queues: dict[str, list[dict]] = {"jobs": [], "dlq": []}
        self.deleted: list[str] = []
        self.visibility: list[tuple[str, int]] = []
        self.received: int = 0
        self.attribute_names: list[list[str]] = []

    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int,
        WaitTimeSeconds: int,
        AttributeNames: "list[str] | None" = None,
    ) -> dict:
        del QueueUrl, WaitTimeSeconds
        self.attribute_names.append(list(AttributeNames or []))
        self.received += 1
        assert MaxNumberOfMessages == 1, "the worker must receive EXACTLY ONE message"
        queue = self.queues["jobs"]
        if not queue:
            return {}
        message = queue.pop(0)
        return {"Messages": [message]}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(QueueUrl)

    def change_message_visibility(self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int) -> None:
        self.visibility.append((ReceiptHandle, VisibilityTimeout))

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict:
        name = "dlq" if "dlq" in QueueUrl else "jobs"
        self.queues[name].append({"Body": MessageBody, "ReceiptHandle": f"rh-{len(self.queues[name])}"})
        return {}


class FakeSecretsClient:
    def __init__(self, by_arn: dict[str, tuple[Mapping, str]], failure: Exception | None = None) -> None:
        self.by_arn = by_arn
        self.failure = failure
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict:
        self.calls.append(SecretId)
        if self.failure is not None:
            raise self.failure
        content, version = self.by_arn[SecretId]
        return {"SecretString": dict_to_bytes(content).decode("utf-8"), "VersionId": version}


WEBHOOK_SECRET_VALUE = "whSEC-test-not-real-000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"


def webhook_secret_content() -> dict:
    return {"secret": WEBHOOK_SECRET_VALUE}


def watermark_secret_content() -> dict:
    return {"secret": "wm-abcdef0123456789"}


def axinom_secret_content() -> dict:
    return {
        "signing_key_hex": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "signing_iv_hex": "0123456789abcdef0123456789abcdef",
        "endpoint": "https://cenc.example.invalid/key",
        "provider_name": "vg-test-provider",
        "scheme": "cbcs",
    }


def fake_secrets(arns: Mapping[str, str], failure: Exception | None = None) -> FakeSecretsClient:
    by_arn: dict[str, tuple[Mapping, str]] = {
        arns["webhook"]: (webhook_secret_content(), "wv1"),
        arns["watermark"]: (watermark_secret_content(), "wmv7"),
        arns["axinom"]: (axinom_secret_content(), "av3"),
    }
    return FakeSecretsClient(by_arn, failure=failure)


class FakeImds:
    """IMDSv2 responder: session token PUT, then instance facts."""

    def __init__(self, instance_id: str = "i-0abc123def4567890a", ami: str = "ami-0123456789abcdef0") -> None:
        self.instance_id = instance_id
        self.ami_id = ami
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        self.calls.append((method, path))
        if method == "PUT" and path == "/latest/api/token":
            assert headers, "token PUT must carry the ttl header"
            return 200, b"ims-token"
        if method == "GET" and path.endswith("instance-id"):
            return 200, self.instance_id.encode()
        if method == "GET" and path.endswith("ami-id"):
            return 200, self.ami_id.encode()
        return 404, b""


class FakeAwsClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 400}}


class FakeAsg:
    def __init__(self, failures: Iterable[tuple[str, int]] | None = None) -> None:
        self.calls: list[dict] = []
        self.failures: deque[tuple[str, int]] = deque(failures or [])

    def terminate_instance_in_auto_scaling_group(self, **kwargs: Any) -> dict:
        self.calls.append(dict(kwargs))
        if self.failures:
            code, times = self.failures.popleft()
            if times > 1:
                self.failures.appendleft((code, times - 1))
            raise FakeAwsClientError(code)
        return {}


# ---------------------------------------------------------------------------
# Clocks, poster, disk
# ---------------------------------------------------------------------------

@dataclass
class Clock:
    """A clock the tests advance manually (monotonic or wall, one class)."""

    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingPoster:
    """Records webhook POSTs; optionally script status codes per call."""

    def __init__(self, statuses: Iterable[int]) -> None:
        self.calls: list[tuple[str, Mapping[str, str], bytes]] = []
        self.statuses: deque[int] = deque(statuses)
        self.fail_all: bool = False
        self.raise_every: int = 0

    def __call__(self, url: str, headers: Mapping[str, str], body: bytes, timeout_sec: float) -> tuple[int, str]:
        del timeout_sec
        self.calls.append((url, dict(headers), body))
        if self.fail_all:
            raise OSError("railway unreachable")
        if self.raise_every and len(self.calls) % self.raise_every == 0:
            raise OSError("poster transport failure")
        return (self.statuses.popleft() if self.statuses else 200), "OK"

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(body) for _url, _headers, body in self.calls]

    @property
    def stages(self) -> list[tuple[str, str, int]]:
        """(stage, qualifier, seq) of each POSTed event in order."""
        return [(b["stage"], b["qualifier"], b["seq"]) for b in self.bodies]


class SleepRecorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeSignaler:
    def __init__(self) -> None:
        self.signals: list[str] = []

    def sigterm(self) -> None:
        self.signals.append("TERM")

    def sigkill(self) -> None:
        self.signals.append("KILL")


# ---------------------------------------------------------------------------
# Subprocess fakes (docker containers, ffmpeg, packager)
# ---------------------------------------------------------------------------

def parses_mounts(argv: Iterable[str]) -> dict[str, tuple[str, str]]:
    """{container_path: (host_path, mode)} from a ``docker run`` argv."""
    mounts: dict[str, tuple[str, str]] = {}
    items = list(argv)
    for index, token in enumerate(items):
        if token != "-v" or index + 1 >= len(items):
            continue
        spec = items[index + 1]
        parts = spec.split(":")
        if len(parts) >= 3:
            mounts[parts[1]] = (parts[0], parts[2])
    return mounts


def completes(stdout: str = "", stderr: str = "", code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def validator_handler(probe_document: dict) -> Callable[[list[str]], SimpleNamespace]:
    """Docker-runner script: the validator container prints its verdict JSON."""

    def handler(argv: list[str]) -> SimpleNamespace:
        mounts = parses_mounts(argv)
        host_input = mounts["/work/input"][0]
        source = Path(host_input) / "source.mp4"
        return completes(
            stdout=json.dumps(
                {"ffprobe": probe_document, "ffprobe_exit": 0, "clamscan_exit": 0, "clam_output": "", "error": ""}
            )
        ) if source.is_file() else completes("invalid input", code=3)

    return handler


def generator_handler(git_sha: str = "b4df00dcafe") -> Callable[[list[str]], SimpleNamespace]:
    """Docker-runner script: the generator writes its 4 atomic outputs."""

    def handler(argv: list[str]) -> SimpleNamespace:
        mounts = parses_mounts(argv)
        out_host = mounts["/work/output"][0]
        out_dir = Path(out_host) / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "variant_a.mp4").write_bytes(b"MASTER-A" * 128)
        (out_dir / "variant_b.mp4").write_bytes(b"MASTER-B" * 128)
        (out_dir / "config.json").write_bytes(b'{"profile": "hevc-v1"}')
        (out_dir / "generation_report.json").write_bytes(b'{"status": "complete"}')
        return completes(stdout=json.dumps(
            {"status": "ok", "git_commit": git_sha, "image_digest": "sha256:deadbeefcafe0123"}
        ))

    return handler


def ffmpeg_handler(available_encoders: str = " h264_nvenc ", fail_when: "str | None" = None) -> Callable[[list[str]], SimpleNamespace]:
    """Runner for the pinned host ffmpeg: probes, encodes, audio, thumbnails.

    ``fail_when`` makes the run exit nonzero whenever the string appears in
    the argv (e.g. ``h264_nvenc`` to simulate a broken GPU encoder, ``y -i``
    to fail everything).
    """

    def handler(argv: list[str]) -> SimpleNamespace:
        if argv[1:3] == ["-hide_banner", "-encoders"]:
            return completes(stdout=f"ffmpeg encoders:{available_encoders}\n")
        if argv[1] == "-version":
            return completes(stdout="ffmpeg version 7.1.5 pinned-test\n")
        if argv[1] == "-y" and "-i" in argv:  # encode, audio extract or thumbnail
            if fail_when is not None and fail_when in " ".join(argv):
                return completes(stderr="broken encoder for this run", code=1)
            target = Path(argv[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"OUT:{target.name}".encode())
            return completes()
        return completes(stderr=f"unhandled ffmpeg argv: {argv[:3]}", code=64)

    return handler


def packager_handler(
    variant_time_offset: int = 977000000,
    uneven_tfdt_for: "str | None" = None,
) -> Callable[[list[str]], SimpleNamespace]:
    """Runner for the host Shaka packager: writes encrypted output trees.

    For every stream descriptor the ARGV carries (``input=...,stream=...``),
    this writes init + two segments under the OUTPUT paths (the descriptor's
    ``init_segment``/``segment_template`` point at the OUTPUT tree, exactly
    like the historical Go worker's OutDir paths). Variant A and B inits
    differ ONLY by their creation_time — canonicalisation must hide that.
    ``uneven_tfdt_for`` (a variant name, e.g. "b") writes mismatching decode
    times so the aligned-timeline gate FAILS.
    """

    def handler(argv: list[str]) -> SimpleNamespace:
        trees: list[tuple[dict, Path]] = []
        for arg in argv:
            if not arg.startswith("input="):
                continue
            fields = dict(part.split("=", 1) for part in arg.split(",") if "=" in part)
            tree = Path(fields["init_segment"]).parent
            trees.append((fields, tree))
            tree.mkdir(parents=True, exist_ok=True)
            kind = fields["stream"]
            # The workspace path is .../packaged/<variant>/<quality>/init.mp4.
            is_variant_a = "/a/" in tree.as_posix()
            time_offset = variant_time_offset if is_variant_a else variant_time_offset + 500
            creation = time_offset if kind == "video" else 42
            tree.joinpath("init.mp4").write_bytes(make_init(creation_time=creation))
            for index in range(1, 3):
                worst_kind = "b" if is_variant_a is False and kind == "video" else "a"
                decode = (index - 1) * 60 + (7 if worst_kind == uneven_tfdt_for and index == 2 else 0)
                tree.joinpath(f"seg_{index:05d}.m4s").write_bytes(make_segment(decode))
        # discardable packager outputs (the worker must NOT publish these)
        for index, token in enumerate(argv):
            path = None
            if token == "--hls_master_playlist_output" and index + 1 < len(argv):
                path = argv[index + 1]
            elif token == "--mpd_output" and index + 1 < len(argv):
                path = argv[index + 1]
            if path is not None:
                Path(path).write_bytes(b"discarded\n")
        return completes(stdout="packager log line\n")

    return handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def worker_config_dict(tmp_path: Path) -> dict:
    """Every required value set to a test-safe placeholder."""
    return {
        "schema_version": 1,
        "aws_region": "us-east-1",
        "s3_bucket": "vg-media-dev",
        "queue": {"jobs_url": "https://sqs.us-east-1.invalid/jobs", "dlq_url": "https://sqs.us-east-1.invalid/dlq",
                  "visibility_timeout_sec": 900, "receive_wait_sec": 20, "max_receive_count": 3},
        "webhook": {"url": "https://railway.example.invalid/webhook", "request_timeout_sec": 1.0,
                    "backoff": [1.0, 2.0, 4.0], "flush_timeout_sec": 5.0},
        "deadlines": {"job_sec": 3600, "heartbeat_sec": 300, "lock_ttl_sec": 1200, "kill_grace_sec": 45},
        "work_dir": str(tmp_path / "work"),
        "log_level": "WARNING",
        "backend": "cuda",
        "pipeline_config_path": str(PIPELINE_CONFIG_PATH),
        "secret_file_dir": str(tmp_path / "secrets"),
        "images": {"generator": "123.dkr.ecr/forensic-wm-generator:hevc-v1-pinned", "validator": "123.dkr.ecr/vg-validator:pinned"},
        "bins": {"docker": "docker", "packager": "packager", "ffprobe": "ffprobe", "ffmpeg": "ffmpeg"},
        "secrets": {"webhook_secret_arn": "arn:aws:secretsmanager:wh", "axinom_secret_arn": "arn:aws:secretsmanager:async",
                    "watermark_secret_arn": "arn:aws:secretsmanager:wm"},
        "asg_name": "vg-workers-dev",
        "prefixes": {"incoming": "incoming/", "jobs": "jobs/", "masters": "masters/", "reports": "reports/",
                     "renditions": "renditions/"},
        "disk": {"headroom_multiplier": 2.5, "headroom_gib": 20},
    }


def build_config(tmp_path: Path, **overrides: Any) -> WorkerConfig:
    data = worker_config_dict(tmp_path)
    for key, value in overrides.items():
        if "." in key:
            section, field_name = key.split(".", 1)
            data.setdefault(section, {})[field_name] = value
        else:
            data[key] = value
    return WorkerConfig.model_validate(data)


def arginom_post_responder() -> Callable[[str, bytes], tuple[int, bytes]]:
    """Axinom http_post double: verify the envelope shape, return one track."""
    response_text = (
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
    import base64

    envelope = json.dumps(
        {"response": base64.b64encode(response_text.encode()).decode("ascii")}, separators=(",", ":")
    ).encode()

    def post(url: str, body: bytes) -> tuple[int, bytes]:
        assert url == axinom_secret_content()["endpoint"]
        assert b"signer" in body and b"request" in body and b"signature" in body
        return 200, envelope

    return post


PROBE_DOCUMENT = {
    "streams": [
        {
            "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
            "pix_fmt": "yuv420p", "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
            "nb_frames": 300, "bits_per_raw_sample": 8, "color_transfer": "bt709",
            "color_primaries": "bt709", "color_range": "tv",
        },
        {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000", "sample_rate": "48000"},
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10.000000"},
}


@pytest.fixture
def world(tmp_path: Path) -> "WorkerWorld":
    """One complete offline worker world for a run() invocation."""
    return WorkerWorld(tmp_path)


@pytest.fixture
def s3() -> FakeS3:
    """A bare fake S3 client for storage-behaviour tests."""
    return FakeS3()


class WorkerWorld:
    """Bundle of every fake + the worker config, built per test."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.s3 = FakeS3()
        self.sqs = FakeSqs()
        self.monotonic = Clock(0.0)
        self.wall = Clock(1_000_000.0)
        self.sleep = SleepRecorder()
        self.poster = RecordingPoster(statuses=())
        self.ffmpeg_calls: list[list[str]] = []
        self.ffmpeg_fail_when: "str | None" = None
        self.nvenc_present = True
        self.validator_probe = dict(PROBE_DOCUMENT)
        self.validator_fails = False
        self.generator_fails = False
        self.generator_exit_code = 0
        self.packager_fails = False
        self.packager_exit_code = 1
        self.packager_uneven_tfdt: "str | None" = None
        self.imds = FakeImds()
        self.asg = FakeAsg()
        self.disk_free_value = 1024 * 1024 * 1024 * 1024  # 1 TiB free
        self.secret_failure: Exception | None = None
        self.signaler = FakeSignaler()
        self.cfg = build_config(tmp_path)
        self.arns = {
            "webhook": self.cfg.secrets.webhook_secret_arn,
            "axinom": self.cfg.secrets.axinom_secret_arn,
            "watermark": self.cfg.secrets.watermark_secret_arn,
        }

    # -- worker configuration ----------------------------------------------

    def reconfigure(self, **overrides: Any) -> WorkerConfig:
        self.cfg = build_config(self.tmp_path, **overrides)
        self.arns = {
            "webhook": self.cfg.secrets.webhook_secret_arn,
            "axinom": self.cfg.secrets.axinom_secret_arn,
            "watermark": self.cfg.secrets.watermark_secret_arn,
        }
        return self.cfg

    # -- docker runner -------------------------------------------------------

    def container_runner(self) -> Callable[[list[str]], SimpleNamespace]:
        def runner(argv: list[str]) -> SimpleNamespace:
            mounts = parses_mounts(argv)
            if "--gpus" in argv and "/work/output" in mounts:  # the generator container
                if self.generator_fails:
                    return completes(stderr="generator boom", code=self.generator_exit_code)
                return generator_handler()(argv)
            if "/work/input" in mounts:  # the validator container
                if self.validator_fails:
                    return completes(stderr="bad source", code=1)
                return validator_handler(self.validator_probe)(argv)
            raise AssertionError(f"container runner got an unknown argv: {argv[:8]}")

        return runner

    # -- ffmpeg / packager ----------------------------------------------------

    def ffmpeg_runner(self) -> Callable[[list[str]], SimpleNamespace]:
        def runner(argv: list[str]) -> SimpleNamespace:
            self.ffmpeg_calls.append(list(argv))
            return ffmpeg_handler(
                available_encoders=" h264_nvenc " if self.nvenc_present else " libx264 ",
                fail_when=self.ffmpeg_fail_when,
            )(argv)

        return runner

    def packager_runner(self) -> Callable[[list[str]], SimpleNamespace]:
        def runner(argv: list[str]) -> SimpleNamespace:
            if self.packager_fails:
                return completes(stderr="packager boom", code=self.packager_exit_code)
            return packager_handler(uneven_tfdt_for=self.packager_uneven_tfdt)(argv)

        return runner

    # -- collaborators ---------------------------------------------------------

    def collaborators(self) -> Collaborators:
        return Collaborators(
            sqs=self.sqs,  # type: ignore[arg-type]
            s3_factory=lambda: self.s3,
            secrets_client=fake_secrets(self.arns, failure=self.secret_failure),
            imds=self.imds,
            asg=self.asg,
            container_runner=self.container_runner(),
            ffmpeg_runner=self.ffmpeg_runner(),
            packager_runner=self.packager_runner(),
            disk_free=lambda path: self.disk_free_value,
            monotonic=self.monotonic,
            wall_clock=self.wall,
            sleep=self.sleep,
            axinom_post=arginom_post_responder(),
            poster=self.poster,
            webhook_executor=None,  # synchronous, deterministic tests
            signaler=self.signaler,
        )

    # -- helpers ----------------------------------------------------------------

    def queue_raw_event(
        self,
        key: str = INCOMING_KEY,
        size: int = SIZE_BYTES,
        attempt: int = 1,
        preload: bool = True,
    ) -> None:
        """Queue the bucket-notification message shape (with sidecar setup)."""
        self.sqs.queues["jobs"].append(
            {
                "MessageId": "msg-raw",
                "Body": json.dumps(raw_s3_event(key, size)),
                "ReceiptHandle": "rh-raw",
                "Attributes": {"ApproximateReceiveCount": str(attempt)},
            }
        )
        if preload:
            preload_job(self.s3, sidecar_dict())

    def queue_canonical(self, epoch: int = 1, attempt: int = 1, retention: str = "archive") -> None:
        message = job_model(run_epoch=epoch, retention=retention)
        self.sqs.queues["jobs"].append(
            {
                "MessageId": f"msg-{epoch}",
                # SQS message bodies arrive as STRINGS in production.
                "Body": dict_to_bytes(message.model_dump()).decode("utf-8"),
                "ReceiptHandle": f"rh-{epoch}",
                "Attributes": {"ApproximateReceiveCount": str(attempt)},
            }
        )
        # A requeue references an upload that already exists; the sidecar may
        # exist too, but the worker must NEVER read it for canonical messages
        # (see test_canonical_message_never_reads_sidecar: it stays absent).
        self.s3.put(INCOMING_KEY, SOURCE_BYTES)

    def dlq_messages(self) -> list[dict]:
        return [json.loads(m["Body"]) for m in self.sqs.queues["dlq"]]
