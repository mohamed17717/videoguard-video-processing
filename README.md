# video-processing — the AWS video pipeline

This is the **standalone repo for the implemented AWS side of the video
pipeline**: the Python packages that run on a GPU worker, the Docker images, the
Packer AMI recipe, the Terraform infrastructure code, the Bunny edge script and
the tests that prove the logic offline. The agreed design lives in
[CONTRACT.md](./CONTRACT.md) — **that file is the single source of truth**; this
README only orients you.

This repo is also registered as a git submodule of `video-guard` at the path
`video-processing/`. Its own root-level CI/CD
(`.github/workflows/aws-pipeline.yml`) is unchanged and runs on pushes to this
repo; a push to `video-guard` only updates the gitlink and does NOT trigger it.
It has one NESTED submodule: `forensic-wm/`, pinned to its own public HTTPS
repo `https://github.com/mohamed17717/videoguard-forensic-watermark.git`.

Everything here was written and unit-tested **offline** (no AWS account, no GPU,
no terraform/packer binary in this environment). Anything that needs real AWS or
a real GPU is listed under **Deferred gates** below and must not be claimed as
verified. The operator-facing deployment guide is
[docs/aws-deployment-runbook.md](./docs/aws-deployment-runbook.md).

## Where this fits in the repository

| Piece | What it is |
|---|---|
| `forensic-wm/` (repo root) | The generator research subtree, a **NESTED git submodule** (NOT a plain directory), pinned to its own public HTTPS repo `https://github.com/mohamed17717/videoguard-forensic-watermark.git`. It contains the forensic-watermark generator and keeps publishing the generator image; nothing in it was renamed or edited. It has its own `AGENTS.md`, `.dockerignore` and `.gitignore`. (`video-guard` pins this whole repo as a git submodule at `video-processing/`.) |
| `video-guard` (SEPARATE GitHub repo) | The **Go control plane**: upload sessions, presigned URLs, webhook receiver, DRM tokens, reconciliation (`internal/awscontrol/`, `internal/drm/`; RabbitMQ and Bunny Stream removed). Its CI is `video-guard/.github/workflows/ci.yml`. Nothing Go lives here. |
| this repo's root (`src/`, `infra/`, `edge/`, `deploy/`, `tests/`, …) | All AWS pipeline code: worker library + agent, images, AMI, Terraform, edge script, tests, docs. |

### Where the Go backend lives

The Go backend (API, database, presigning, webhooks, DRM token issuer) is NOT
in this repository. It stayed in
[`https://github.com/mohamed17717/video-guard`](https://github.com/mohamed17717/video-guard)
with its own CI (`video-guard/.github/workflows/ci.yml`), where
`go build ./... && go test ./...` runs. This repo calls into it (webhooks,
playback tokens) but never compiles it.

## Why `forensic-wm/` is a nested submodule

The generator keeps its own repository, its own history and its own pinned
commit: `https://github.com/mohamed17717/videoguard-forensic-watermark.git`,
pinned in this repo's `.gitmodules`. A **git submodule** is a pointer to one
pinned commit of a second repository, so a fresh clone needs
`git clone --recurse-submodules` (or `actions/checkout` with
`submodules: recursive`); GitHub "Download ZIP" and `git archive` do NOT
include submodule contents. The submodule URL is public HTTPS, so no SSH
deploy key and no `webfactory/ssh-agent` step are needed. The generator image
build context is unchanged at `forensic-wm/`, so its `.dockerignore` allowlist
keeps the extractor, tests, samples and `.env` out of the published image.

## The tree

```
video-processing/
├── CONTRACT.md                 # agreed baseline — the single source of truth
├── README.md                   # this file
├── AGENTS.md                   # short working rules for agents in THIS repo
├── .dockerignore               # excludes forensic-wm/ from the ROOT (validator)
│                               #   build context; generator context unaffected
├── docs/
│   └── aws-deployment-runbook.md   # operator runbook (phases, ops, rollback)
├── pyproject.toml              # Python package metadata (vg_pipeline + vg_worker)
├── requirements-worker.txt     # runtime Python deps for the worker agent
├── configs/
│   ├── pipeline.yaml           # profile, 4-quality ladder (720/480/360/240p),
│   │                           #   DRM + input contract + hard caps (CONTRACT §4)
│   └── aws.worker.yaml         # worker runtime settings (prefixes, deadlines,
│                               #   queue URLs placeholders, image refs, bins)
├── src/
│   ├── vg_pipeline/            # pipeline library: contracts, probe, ClamAV,
│   │                           #   ladder, CENC, MP4 box walking, Shaka, artifacts
│   │                           #   (contracts.py, probe.py, clamav.py, ladder.py,
│   │                           #    cenc.py, mp4.py, shaka.py, artifacts.py,
│   │                           #    errors.py, logging.py)
│   └── vg_worker/              # the worker agent: SQS receive/normalise, S3 I/O,
│       │                       #   secrets, webhooks, supervisor, self-termination
│       │                       #   (main.py, normalize.py, s3io.py, secrets.py,
│       │                       #    webhooks.py, supervisor.py, terminate.py,
│       │                       #    config.py, container.py)
│       └── stages/             # one module per stage: validate, generate,
│                               #   ladder, package, finalize
├── deploy/
│   ├── validator/Dockerfile    # network-less ffprobe + ClamAV validation image
│   │                           #   (build context = repo root, see .dockerignore)
│   ├── worker/                 # host systemd agent: install.sh, entrypoint.sh,
│   │                           #   worker-agent.service (runs OUTSIDE Docker)
│   └── packer/                 # worker.pkr.hcl + variables.pkr.hcl — the AMI
├── infra/                      # Terraform (see infra/README.md): S3, SQS+DLQ,
│   │                           #   IAM, ECR, ASG, security groups, CloudWatch,
│   │                           #   Bunny pull zone, Cloudflare DNS, state bootstrap
│   └── .../*.tf, bootstrap-state.sh, terraform.tfvars.example
├── edge/bunny/                 # Bunny Edge Scripting script (TypeScript) +
│   │                           #   Deno unit tests + README (see its README.md)
│   ├── src/                    # ab_pattern.ts, token.ts, manifest.ts, origin.ts, index.ts
│   └── tests/                  # ab_pattern_test.ts, manifest_test.ts,
│                               #   origin_test.ts, token_test.ts
├── tests/
│   ├── pipeline/               # vg_pipeline unit tests (incl. Axinom golden vectors)
│   ├── worker/                 # vg_worker unit tests (all failure paths)
│   └── infra/                  # python-hcl2 parse tests + CI-YAML parse test
├── .github/workflows/
│   └── aws-pipeline.yml        # THIS repo's CI (pytest, Deno, images, packer,
│                               #   edge deploy, terraform validate/apply)
└── forensic-wm/                # generator research subtree — NESTED git
    │                           #   submodule (public HTTPS); own AGENTS.md/
    │                           #   .dockerignore/.gitignore; Docker build
    │                           #   context for the generator image
    └── ...                     # (untouched by the pipeline work)
```

(`__pycache__/` directories are local Python bytecode noise, not source.)

## Technology stack

| Layer | Technology |
|---|---|
| Pipeline library + worker agent | **Python 3.12**, type hints, pydantic v2 contracts, boto3, structured JSON logging |
| Control plane (`video-guard` repo, NOT here) | **Go** — `internal/awscontrol` (presign + job sidecar, SQS, webhook sign/verify + handler, reconciliation, variant assignment) and `internal/drm/token.go` (Axinom DRM token) |
| Containers | **Docker** images pushed to **ECR**: `forensic-wm-generator:hevc-v1-<sha>` (context = `forensic-wm/`) and `vg-validator:pinned-<sha>` (context = this repo root `.`) |
| Worker host | **Packer** AMI (Ubuntu): NVIDIA driver ≥ 550.54.14 + Container Toolkit, pinned CloudWatch agent, pinned **Shaka Packager** at `/usr/local/bin/packager`, a **second pinned FFmpeg 7.1.5** build (`libx264` + `h264_nvenc`) — the generator image has no `libx264` — pyenv Python 3.12, and the host systemd worker agent |
| Infrastructure | **Terraform** (AWS + Cloudflare providers; Bunny is managed through a `null_resource` calling the Bunny API with a sensitive token, not a community provider) |
| Delivery personalisation | **Bunny Edge Scripting** written in TypeScript, tested with **Deno** |
| CI | `.github/workflows/aws-pipeline.yml` **IN THIS REPO** — OIDC (no static AWS keys), pytest + Deno gates, image build/push, gated `packer build`, gated `terraform apply`. The Go backend's CI is `video-guard/.github/workflows/ci.yml` in the `video-guard` repo |

## The 8-stage worker flow

The worker agent is a **host systemd service** (not a container with access to
`/var/run/docker.sock`). Each video runs on its own ephemeral `g6.xlarge`
instance that terminates itself afterwards. The flow, from CONTRACT.md §8:

1. **Receive** — pull exactly ONE message from the SQS jobs queue
   (`MaxNumberOfMessages=1`), normalise it (raw S3 event or backend `JobMessage`),
   read the `jobs/<job_id>/job.json` sidecar, acquire the conditional
   `reports/<job_id>/_LOCK` (`If-None-Match: *` — a second concurrent delivery of
   the same job gets HTTP 412 and terminates instead of running a second GPU
   job), and start the 3600 s job deadline plus the 300 s heartbeat
   (`ChangeMessageVisibility` + lock renewal; the queue visibility timeout is
   900 s, so a dead worker's message returns in ~15 min).
2. **`validated`** — download the source to a read-only `/work/input`, run
   ffprobe + ClamAV inside the network-less `validator` container against the
   full input contract and hard caps; any violation fails closed to the DLQ.
3. **`masters_ready`** — run the `generator` container (`--gpus all`,
   `--network=none`, `--read-only`, `--cap-drop ALL`, non-root UID 10001, tmpfs
   secret, `--backend cuda`) to produce the A (+watermark) and B (−watermark)
   masters; verify the atomic outputs and publish the reports.
4. **ladder** — encode 2 variants × produced qualities (720/480/360/240p; sizes
   above the source are skipped) + 6 thumbnails; upload each with the storage
   class chosen from the sidecar `retention` field; one webhook per quality.
5. **`packaged`** — ONE Axinom key request, then ONE Shaka Packager run with 9
   stream descriptors (8 video: a/b × 4 qualities, + 1 audio) sharing a single
   CENC KID/KEY; canonicalise the init segments (zero `creation_time` /
   `modification_time`) and publish ONE canonical init per quality; enforce the
   aligned-timeline gate (equal segment counts, equal `tfdt` base times,
   byte-identical normalised inits) with no fallback; write the canonical HLS +
   DASH manifests and upload them.
6. **`done`** — tag the original `processed=true` (archive retention only), POST
   the bounded `done` webhook, write `reports/<job_id>/_DONE` **after** the
   webhook attempt, delete the SQS message.
7. **Terminate** — call
   `autoscaling:TerminateInstanceInAutoScalingGroup` with
   `--should-decrement-desired-capacity` on its own instance id; retry 3×, then
   `shutdown -h now`. The worker never polls a second message.
8. **On any failure** — send the `failed` webhook (final attempt only);
   deterministic failures (`INPUT_CONTRACT`, `CLAMAV`, `GENERATOR_EXIT_2`,
   `PACKAGING_*`, `SIDECAR_INVALID`) go to the DLQ immediately, transient ones
   go back to the queue; terminate the instance. No partial output is published
   and no instance is reused.

Webhooks are HMAC-signed (`ts.job_id.stage.seq.body`, 300 s replay window) and
Railway performs a synchronous **conditional upsert** ordered by
`(run_epoch, attempt, seq)` — never `ON CONFLICT DO NOTHING`; `done` is terminal.

## Local verification (what actually ran here)

Offline tests only — no AWS credentials, GPU, Shaka binary, Axinom or Bunny
runtime exist in this environment (see CONTRACT.md §11):

```bash
# Python pipeline + worker + infra parse tests (Python 3.12 via pyenv)
~/.pyenv/versions/3.12.0/bin/python3.12 -m pytest tests -q

# Bunny edge script unit tests (offline, injectable fetch/clock)
deno test --allow-read edge/bunny/tests/
deno check edge/bunny/src/*.ts

# Go control plane — runs in the video-guard repo, NOT here
cd ../video-guard && go build ./... && go test ./...
```

Terraform/Packer files are **parse-only** here (`python-hcl2`); `terraform
validate`, `terraform plan`, `packer validate` and `packer build` run in CI or
with a real account.

## Deferred gates (needs real AWS / g6.xlarge) — from CONTRACT.md §11

> Attributed to CONTRACT.md §11 — **do not claim any of this as done locally.**
> `terraform validate` and `packer validate` are CI gates in this repo's
> workflow, but they have never RUN here (no terraform/packer binary in the
> implementation environment).

No AWS credentials, no terraform/packer/aws CLI, no g6.xlarge/L4 GPU, no Shaka
binary, no Axinom credentials, no real Bunny edge runtime in this environment.

- **AWS:** S3 event → SQS queue policy/shape; presigned PUT from a real browser
  with CORS and exact `Content-Length`; conditional `_LOCK`; ASG scale-out and
  self-termination; DLQ redrive; Glacier IR billing / 128 KiB minimum / 90-day
  minimum; ECR, Secrets Manager, CloudWatch; IAM least privilege; Bunny origin
  signing.
- **GPU:** `make gpu-acceptance GPU_BACKEND=cuda` on a real `g6.xlarge`; NVENC
  ladder; real 1-hour packaging; init canonicalisation against the real Shaka
  muxer; Widevine/PlayReady/FairPlay playback; 4K/HDR/VFR handling.
- **Terraform/Packer:** `validate`/`plan`/`apply`/`build` in CI or a real
  account. `python-hcl2` is parse-only.
- **Shaka:** not installed locally; synthetic smoke test only.
- **Axinom:** golden-vector tests only; live key request / license issuance /
  FairPlay SPC-CKC are gates.
- **Bunny Edge/CDN:** Deno unit tests only; real edge runtime, origin signing and
  cache behaviour are gates.
- **ClamAV:** validator image + EICAR test is a gate.
- **Toolchain note:** `python3` is 3.11.8; use `pyenv` 3.12.0
  (`~/.pyenv/versions/3.12.0/bin/python3.12`) with pytest/pyyaml/pydantic/boto3/
  python-hcl2. Host FFmpeg is 9.0.2 while the generator image pins 7.1.5 — never
  use host FFmpeg to certify the image.
