"""Worker agent for the AWS video pipeline (CONTRACT.md sections 2, 5, 8).

The agent is a **host systemd service**, not a container. It receives EXACTLY
ONE SQS message, runs the full media pipeline once (download -> validate ->
generate -> ladder -> package -> finalize), deletes the message (or routes it
to the DLQ for deterministic failures), and terminates its own EC2 instance.
It never polls a second message.

Every AWS, subprocess, HTTP, docker and filesystem side effect goes through
injectable collaborators so tests run fully offline with fakes.
"""

from .config import WorkerConfig, load_worker_config
from .normalize import TestEventIgnored, normalize_message
from .s3io import S3Client, S3IO, storage_class_for_object
from .secrets import FetchedSecrets, fetch_secrets, write_watermark_tmpfs
from .webhooks import Emitter, WebhookEmitter, sign_body, verify_signature
from .container import ContainerError, run_generator, run_validator
from .supervisor import DeadlineExceeded, Supervisor
from .terminate import terminate as terminate_instance

# NOTE about the entry point: the CLI module ``vg_worker.main`` is NOT
# imported here on purpose — importing the submodule eagerly makes
# ``python -m vg_worker.main`` warn about double imports. Use
# ``from vg_worker.main import run, Collaborators, main`` when needed.

__version__ = "1.0.0"

__all__ = [
    "ContainerError",
    "DeadlineExceeded",
    "Emitter",
    "FetchedSecrets",
    "S3Client",
    "S3IO",
    "Supervisor",
    "TestEventIgnored",
    "WebhookEmitter",
    "WorkerConfig",
    "fetch_secrets",
    "load_worker_config",
    "normalize_message",
    "run_generator",
    "run_validator",
    "sign_body",
    "storage_class_for_object",
    "terminate_instance",
    "verify_signature",
    "write_watermark_tmpfs",
]
