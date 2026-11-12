# AWS deployment runbook (operator)

Step-by-step guide for the person deploying the video pipeline to AWS. Written
in simple English; every technical term is explained the first time it appears.

> **Read this first — honest status.** The pipeline **code** exists and passes
> offline tests in this repository,
> `https://github.com/mohamed17717/videoguard-video-processing` (clone that URL;
> this repo has ONE **nested** submodule: `forensic-wm/`, pinned to its own
> public HTTPS repo, and this repo itself is registered as a submodule of
> `video-guard`). See the [README](../README.md) and
> [CONTRACT.md](../CONTRACT.md) §11. **No real AWS resource was created in the
> session that wrote this runbook**: no bucket, queue, role, AMI, ECR repo,
> CloudWatch alarm, Bunny zone or GPU box exists because of it. `terraform`/
> `packer`/`aws` CLI were never run against a real account, and no
> `g6.xlarge`/L4 GPU acceptance ran. **This runbook is for the operator** — every
> step below is still to be performed and verified by a human with real
> credentials. Anything marked **DEFERRED GATE** must not be treated as done.

## Prerequisites

| Need | Detail |
|---|---|
| This repository | `git clone --recurse-submodules https://github.com/mohamed17717/videoguard-video-processing.git` — a recursive checkout; NO SSH deploy key (`forensic-wm/` is a public HTTPS submodule pinned in `.gitmodules`). |
| AWS account | With permissions to create S3, SQS, IAM, ECR, EC2/ASG, Secrets Manager, CloudWatch, DynamoDB resources in **us-east-1** (the region every file assumes). |
| Tools installed | `terraform` (CI pins **1.9.8**), `packer` (CI pins **1.11.2**), `aws` CLI v2, `docker` (for local image builds), `git`. |
| GitHub secrets/vars | `AWS_OIDC_ROLE_ARN`: an IAM role assumed via OIDC (short-lived, keyless credentials) for ECR push, Packer and terraform. `BUNNY_API_KEY`: Bunny account API key for edge deploy. `EDGE_TOKEN_SECRET` (secret) + `EDGE_ORIGIN_HOST` (variable): written to the Bunny script as `VG_EDGE_TOKEN_SECRET` / `VG_EDGE_ORIGIN_HOST` — CI only, never Terraform. `TF_STATE_BUCKET`: the bucket from Phase 1. |
| External accounts | **Bunny** (CDN pull zone + Edge Scripting), **Cloudflare** (DNS zone + API token), **Axinom** (multi-DRM: communication key, signing key + IV, license endpoint), **Railway** (the Go backend host). |
| Secrets values | Axinom signing key/IV + endpoint, the watermark secret, a webhook signing secret — created in Phase 2, never committed. |

## Phase 0 — GPU acceptance gate FIRST (BLOCKING)

Run this **before** anything else, on a real AWS `g6.xlarge` (L4 GPU). Nothing
below is trustworthy until it passes, because **no L4/CUDA certification exists
yet** and the cost model rests on unmeasured numbers:

```bash
# inside the forensic-wm/ directory of a checkout of this repo, on the g6.xlarge box
make gpu-acceptance GPU_BACKEND=cuda
```

- CUDA (NVIDIA's GPU programming stack) results are the only thing that
  certifies the AWS worker; the macOS **Metal results do not certify CUDA**.
- The generator's master-encode quality (CQ 18) is **provisional** — unmeasured
  on L4 — and the rendition ladder's `nvenc_cq` values (23/24/25) are likewise
  unmeasured.
- From this run, **measure and record**: real artifact sizes (masters,
  renditions, total), real wall time per stage (boot, validated, GPU, packaging),
  and the real number of PUT requests (a 1-hour title at 2 s segments is
  ~14–16k small objects). PRICING.md assumes ~10 GB total artifacts and ~24 min
  wall time — **do not trust those numbers until this run measures them**. If
  measured sizes exceed the model by >2×, start a repricing round before
  provisioning anything sized from the model.

## Phase 1 — Bootstrap Terraform state

Terraform stores its **state** (the record of what it created) in S3 and locks
it with a DynamoDB table so two operators cannot apply at once. One of the two
credential-free bootstrap steps:

```bash
cd infra
./bootstrap-state.sh dev        # creates vg-tfstate-dev + vg-tfstate-lock (idempotent)
```

Then initialise and validate locally:

```bash
terraform init -backend-config="bucket=vg-tfstate-dev"     # or edit backend.tf REPLACE_WITH_ENV
cp terraform.tfvars.example terraform.tfvars               # fill in real values; NEVER commit it
terraform validate
terraform plan
```

`terraform validate`/`plan` with real credentials is a **deferred gate**
(`python-hcl2` parse tests are the only terraform verification that ran
offline). The real `terraform apply` runs in CI (job `terraform-apply`) behind a
manual approval on the `production` environment — do not apply production from a
laptop.

## Phase 2 — Secrets, ECR images, AMI

1. **Create the three Secrets Manager secrets** (values only; Terraform gets the
   ARNs, the worker fetches the values at run time):
   - `vg/webhook` — `{"secret":"..."}` (the Railway backend holds the same value
     as a Railway secret so it can verify worker webhooks),
   - `vg/axinom` — `{"signing_key_hex":"...","signing_iv_hex":"...","endpoint":"...","provider_name":"...","scheme":"cbcs"}`,
   - `vg/watermark` — `{"secret":"..."}`.
   Put the three ARNs into `terraform.tfvars`.
2. **Build + push the two ECR images** (CI job `images` does this with OIDC, or
   by hand):
   ```bash
   docker build --platform=linux/amd64 \
     -f forensic-wm/deploy/generator/Dockerfile \
     -t <ecr>/forensic-wm-generator:hevc-v1-<sha> forensic-wm        # context = forensic-wm
   docker build --platform=linux/amd64 \
     -f deploy/validator/Dockerfile \
     -t <ecr>/vg-validator:pinned-<sha> .                            # context = repo root
   ```
   The generator build context **must stay `forensic-wm/`** so its
   `.dockerignore` allowlist applies (the root `.dockerignore` does not affect
   that context). The validator context is the repo root, which the root
   `.dockerignore` keeps free of `forensic-wm/`. Note the generator image is
   GPU-only and has **no `libx264`** — the CPU fallback FFmpeg is baked into
   the AMI next.

   A plain `git clone` leaves `forensic-wm/` empty; always clone with
   `--recurse-submodules`.

   **COLD-START ORDERING (first deployment only).** The CI dependency chain is
   `images → packer → terraform-apply`: the `images` job pushes to ECR, the
   `packer build` job pre-pulls those exact image tags into the AMI, and
   `terraform-apply` receives the full image refs as `-var generator_image` /
   `-var validator_image` plus the built AMI id. That means the ECR
   **repositories must already exist before `images` pushes** — on a brand-new
   account Terraform has not run yet, so create the two repositories once by
   hand (`aws ecr create-repository --repository-name forensic-wm-generator`,
   same for `vg-validator`) or run a bootstrap `terraform apply` that stops at
   the ECR layer. After the first cycle, Terraform owns the repositories and
   the CI chain is self-sustaining. Pushing into a non-existent repository,
   or running `packer build` before the images are pushed, fails the job.
3. **Build the worker AMI** (a machine image every worker boots from):
   ```bash
   cd deploy/packer
   packer init .
   packer validate .
   packer build -var "generator_image=<ecr>/forensic-wm-generator:hevc-v1-<sha>" \
                -var "validator_image=<ecr>/vg-validator:pinned-<sha>" .
   ```
   The AMI bakes: NVIDIA driver ≥ 550.54.14 + Container Toolkit, CloudWatch
   agent, pinned Shaka Packager, the **second pinned FFmpeg 7.1.5**
   (`libx264` + `h264_nvenc`) at `/usr/local/bin/{ffmpeg,ffprobe}`, pyenv Python
   3.12, the host systemd worker agent, and labels (`vg:build-date`,
   `vg:ffmpeg-sha`, `vg:shaka-version`). Put its AMI id into
   `terraform.tfvars` (`ami_id`). `packer validate`/`build` with real AWS is a
   **deferred gate**.
4. **`terraform apply`** (CI-gated) creates the S3 bucket, SQS queue + DLQ, IAM
   roles, ECR repos, the ASG (min 0 / max 3, `g6.xlarge`, scale-OUT only), the
   egress-only security group, CloudWatch log groups + five alarms, the Bunny
   pull zone shell and the Cloudflare CNAME. Terraform outputs give the bucket /
   queue names, role ARNs and the sensitive access keys.

## Phase 3 — Bunny edge + DNS

1. **Deploy the edge script code** (CI job `edge`, using `BUNNY_API_KEY`). The
   job REBUILDS `edge/bunny/deploy/entry.js` from `src/middleware.ts` with
   `deno bundle` on every run (an empty bundle or a missing `__VG_VARIANT__`
   token fails the job), then creates or updates the Bunny compute script,
   writes `VG_EDGE_TOKEN_SECRET` / `VG_EDGE_ORIGIN_HOST` through the documented
   variables/secrets endpoints, publishes the release, and attaches it to the
   pull zone with `MiddlewareScriptId` + `EdgeScriptExecutionPhase: 2`. The
   built bundle is also a tracked artifact in this repo. **DEFERRED RUNTIME
   GATE:** the actual Bunny API deploy is gated on
   `vars.EDGE_DEPLOY_ENABLED == 'true'`; when the gate is off the job prints a
   visible `::warning::` and does NOT publish.
2. **Pull zone + AWS signing**: `infra/bunny.tf` creates the pull zone via
   `null_resource` + curl against `https://api.bunny.net` (community Terraform
   providers were rejected). Paste the `vg-bunny-origin-<env>` IAM user
   credentials (from `terraform output bunny_origin_access_key_id`; the secret
   is a sensitive output) into the Bunny dashboard's AWS signing settings
   (`AWSSigningEnabled`), enable Origin Shield, and set cache rules so
   `*.m3u8`/`*.mpd` bypass cache. **DEFERRED GATE:** verify the exact Bunny API
   field names against docs.bunny.net before the first apply.
3. **Cloudflare DNS**: `cloudflare.tf` creates the CNAME for the playback
   hostname (`pull_zone_hostname`, e.g. `media`) in your zone
   (`cloudflare_zone_id` + `cloudflare_api_token` in `terraform.tfvars`).

## Phase 4 — Railway wiring

The Go backend on Railway is the control plane. Set (as Railway secrets/env):

- The Railway **IAM user** access key + secret (sensitive terraform outputs;
  scope: `s3:PutObject` on `jobs/*` + `incoming/*`, `s3:GetObject` on `jobs/*`,
  `s3:GetObject` on `incoming/*` (the reconciler calls `HeadObject` on
  `incoming/<job_id>/source.mp4` to detect abandoned uploads), `s3:GetObject`
  on `reports/*` + `renditions/*`, `s3:ListBucket` scoped by an
  `s3:prefix` condition to `reports/*` and `renditions/*`, and
  `sqs:SendMessage` on the jobs queue — rotate on a schedule). The full list is
  in `CONTRACT.md` §1 and `infra/iam.tf`.
- Queue URLs / bucket / region (or fetch from SSM) and the **webhook secret
  value** (same secret the worker reads from Secrets Manager).
- The Axinom **communication key** for `internal/drm/token.go` (DRM playback
  tokens), and the playback-token signing secret.

Wired behaviour, already implemented in Go:

- `StartUploadSession` creates the `job_id`, **writes
  `jobs/<job_id>/job.json` BEFORE returning**, then returns the presigned PUT
  (`expires_at` 900 s) with `headers: {"Content-Type":"video/mp4"}`; the
  signature covers `content-length;content-type;host` — NOT
  `x-amz-storage-class` (the browser upload stays STANDARD).
- The webhook route (`/internal/pipeline/webhook`, set as
  `webhook_host`/`webhook_path` in `terraform.tfvars`) verifies
  HMAC + 300 s replay window and does the synchronous conditional upsert on
  `(job_id, stage, qualifier)` ordered by `(run_epoch, attempt, seq)`.
- Playback returns `hls_manifest_path` / `dash_manifest_path` and a signed
  playback token; the backend logs `session_id → seed` at issuance.

## Phase 5 — The full flow (what to expect)

1. Backend writes `jobs/<job_id>/job.json` (sidecar: `video_public_id`,
   `content_id`, `retention`, `run_epoch`, …), returns the presigned PUT.
2. Browser PUTs the source to `incoming/<job_id>/source.mp4`.
3. `s3:ObjectCreated:Put` on `incoming/*.mp4` → SQS `vg-video-jobs-<env>`
   (visibility **900 s**; deterministic failures → DLQ immediately;
   `maxReceiveCount=3` otherwise).
4. ASG scales **out only** on visible messages; a `g6.xlarge` boots the Packer
   AMI (~2–4 min) and the worker agent takes the message.
5. Worker: `_LOCK` → validated → masters → ladder → packaged → done, per the
   8-stage flow in the [README](../README.md).
6. Webhooks (`validated`, `masters_ready`, `rendition_ready` per quality,
   `packaged`, `done`, `failed`) hit Railway; `_DONE` is written after the
   bounded `done` POST; the instance self-terminates.
7. Playback: viewer requests the manifest with `?vg_token=`; the Bunny edge
   script verifies the token, substitutes the A/B variant (HLS per segment,
   DASH one variant per session) and serves a personalised `no-store` manifest;
   segments and thumbnails pass through, cached and immutable.

**S3 key map** (who can read what: only `renditions/*` is reachable by the Bunny
origin identity):

| Key | Content | Class |
|---|---|---|
| `jobs/<job_id>/job.json` | job sidecar (written before the presign) | Standard, never CDN |
| `incoming/<job_id>/source.mp4` | raw browser upload ("original") | Standard during job; Glacier IR after `processed=true` (archive) |
| `masters/<job_id>/variant_{a,b}.mp4` | watermarked A/B masters | Glacier IR after success |
| `reports/<job_id>/…` (`config.json`, `generation_report.json`, `manifest.json`, `_LOCK`, `_DONE`) | evidence + lock/completion markers | Standard |
| `renditions/<job_id>/init/<quality>/init.mp4` | ONE canonical time-normalised init per quality | Glacier IR day 0, CDN |
| `renditions/<job_id>/{a,b}/<quality>/seg_NNNNN.m4s` | A/B CMAF segments | Glacier IR day 0, CDN |
| `renditions/<job_id>/audio/{init.mp4,seg_NNNNN.m4s}` | shared audio tree | Glacier IR day 0, CDN |
| `renditions/<job_id>/hls/*.m3u8`, `dash/stream.mpd` | canonical manifests | Standard, CDN (personalised at edge) |
| `renditions/<job_id>/thumbs/thumb_NN.jpg` | 6 thumbnails | Standard, CDN |

Retention is chosen per job from the sidecar: `archive` → GLACIER_IR from day 0
+ tag the original `processed=true`; `ttl30` → STANDARD + 30-day expiry, **never
Glacier** (the 90-day minimum would outlive the TTL).

Wall-time expectations (~22–25 min total) are the **unmeasured** model numbers —
replace them with the Phase 0 measurements.

## Phase 6 — Operations

- **DLQ redrive.** A CloudWatch alarm fires on DLQ depth > 0. Inspect the
  message's `error_code`: deterministic codes (`SIDECAR_INVALID`,
  `INPUT_CONTRACT`, `CLAMAV`, `GENERATOR_EXIT_2`, `PACKAGING_*`) mean the job
  can never succeed as-is — fix the cause, then **requeue from the admin
  action**, which publishes a NEW `JobMessage` with a NEW `run_id` and a greater
  `run_epoch` (never just replay the old message).
- **Reconciliation.** If every webhook fails, files still land in S3; the
  backend rebuilds video status from `reports/` + `renditions/`
  (`internal/awscontrol/reconcile.go`). Use it when a video looks stuck.
- **CloudWatch alarms** (five, in `infra/cloudwatch.tf`): job duration, non-zero
  worker exits, DLQ depth, webhook failure rate, unusual worker egress. Tune
  thresholds against real traffic — that tuning is a deferred gate.
- **Monthly re-pin / rebuild.** Rebuild the ECR images and the AMI when FFmpeg,
  OpenCV, NumPy, Taichi or Debian advisories affect a pin; ECR scan-on-push
  findings are a rebuild trigger, never a runtime install. Rotate the Railway
  IAM key and the Bunny origin key. Keep the pin chain one-directional:
  Packer variables → AMI tags → launch-template user_data → `manifest.json`.
- **PRICING.md monthly watch list.** Real fill bytes per rendition, fill egress
  vs the ~$75/TB cutoff, actual cost per video vs ~$0.42, catalog growth vs
  day-0 Glacier IR + small-object overhead, licenses/session ratio and measured
  artifact size (any drift >2× → repricing round two).

## Phase 7 — Rollback and acceptance gates

**Rollback levers:**

- **Workers:** set ASG desired capacity to 0 (nothing else is always-on).
- **AMI:** keep the previous AMI id in `terraform.tfvars`; ECR tags are
  immutable, so rolling back an image = pointing the launch template at the
  prior pinned digest and re-applying.
- **Infra:** applies are CI-gated; revert the `.tf` commit and re-run the apply
  job (state is versioned in S3).
- **Edge:** republish the prior `entry.js` via the Bunny API (or delete the
  script — canonical manifests remain correct, just unpersonalised).
- **DNS:** disable the Cloudflare CNAME to take playback offline without
  touching storage.

**Acceptance gates before real uploads** (from `pricing/SECURITY.md` §12 plus
the GPU gate):

```bash
~/.pyenv/versions/3.12.0/bin/python3.12 -m pytest tests -q   # offline suite (this repo)
deno test --allow-read edge/bunny/tests/                     # edge suite
go build ./... && go test ./...                              # control plane (video-guard repo)
python -c "import hcl2; [hcl2.load(f) for f in __import__('glob').glob('infra/*.tf')]"  # parse-only locally
terraform plan            # with real credentials (CI)
packer validate .         # in deploy/packer
make gpu-acceptance GPU_BACKEND=cuda   # in forensic-wm/, on a real g6.xlarge — BLOCKING
```

Plus the SECURITY.md §12 runtime checks: a deliberately malformed upload is
rejected by ffprobe before the generator; the parsing container shows no egress
in CloudWatch; a killed job leaves no partial output and a DLQ entry; no static
AWS keys on any worker (OIDC + instance profiles only).

## Deferred-gate table

Every gate that was NOT verified in the implementation session (CONTRACT.md
§11). "Blocking?" = blocks real customer uploads.

| Gate | How to verify | Blocking? |
|---|---|---|
| `terraform validate` / `plan` / `apply` | CI jobs `terraform` / `terraform-apply` with real credentials | Yes (nothing exists without it) |
| `packer validate` / `build` | CI job `packer` (build gated on `production` env) | Yes |
| ECR push/pull, Secrets Manager reads, CloudWatch agent | CI `images` job + first worker boot logs | Yes |
| S3 event → SQS shape + queue policy | Upload one real file; confirm one visible message | Yes |
| Presigned PUT from a real browser (CORS, exact `Content-Length`) | Real upload session end-to-end | Yes |
| Conditional `_LOCK` (no duplicate GPU jobs) | Send the same job twice concurrently; second must get 412 + terminate | Yes |
| ASG scale-out + worker self-termination | Queue 2–4 jobs; watch instances launch and terminate; desired returns to 0 | Yes |
| DLQ redrive + admin requeue (new `run_epoch`) | Force a deterministic failure; requeue | Yes |
| Glacier IR day-0 transition; 128 KiB / 90-day minimums; PUT cost | First real bill | Cost-model input, not launch-blocking |
| `make gpu-acceptance GPU_BACKEND=cuda` on `g6.xlarge` | Phase 0 | **Yes — Phase 0, first** |
| NVENC ladder + real 1-hour packaging wall time | Phase 0 timing + `done` payload | Yes (validates the cost/latency model) |
| Init canonicalisation vs the real Shaka muxer | One real `packaged` stage; aligned-timeline gate green | Yes |
| Widevine/PlayReady/FairPlay playback | Real player + Axinom license | Yes |
| Axinom live key request / license issuance / FairPlay SPC-CKC | First real license | Yes |
| Bunny edge runtime (bypass header, cache rules, `entry.js` bundle) | Deploy + real playback through the zone | Yes |
| Bunny API payload field names (pull zone via Terraform; edge script create/publish/params via the CI `edge` job) | First `terraform apply` with `bunny_api_key` set, and the first enabled CI `edge` deploy | Yes |
| Cloudflare record + custom-hostname TLS | `cloudflare.tf` apply + HTTPS playback | Yes |
| ClamAV validator image + EICAR test | Drop an EICAR-named sample through the validator | Yes |
| IAM least privilege (worker/Railway/Bunny policies in practice) | `aws iam simulate-principal-policy` or access-denied smoke tests | Yes |

## What this session did and did not do

**Done:** all pipeline code, Dockerfiles, Packer/Terraform files, the edge
script, and the offline test suites (Python via pyenv 3.12, Deno) — all in this
repo (`video-processing`, branch `main`, also a submodule of
`video-guard`; its `forensic-wm/` is a nested submodule), all left
uncommitted for the owner to review and push. The Go control-plane changes
live in the separate `video-guard` repo.

**Not done:** no AWS resource was created, no credentials were used, no
`terraform`/`packer`/`aws` command was executed against a real account, and no
GPU acceptance ran. **This runbook is the operator's checklist for doing that
work.** Nothing here should be quoted as verified until the deferred-gate table
above is green in a real environment.
