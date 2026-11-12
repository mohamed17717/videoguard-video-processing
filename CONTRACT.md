# AWS pipeline contract — v2 (AGREED BASELINE)

> Single source of truth for all agents implementing the AWS video pipeline.
> Every file, message, key, flag and name below is a contract: implementers must
> not invent their own. Simple English; technical terms explained on first use.
>
> v1 incorporates the DeepSeek critical-design review (D1 AGREE with amendments,
> D2 DISAGREE → replaced packaging design, D3 AGREE with ordering amendments,
> plus 23 numbered defects). Do not silently deviate; raise it with the
> orchestrator first.

## 0. Repository layout (D1 — AGREED, with amendments)

This is the **standalone `videoguard-video-processing` repository**
(`https://github.com/mohamed17717/videoguard-video-processing`). It is also
registered as a git submodule of `video-guard` at `video-processing/`;
all paths below are relative to THIS repo's root, which is unchanged by that
registration. The former
`video-processing/` subtree of `video-guard` moved to this repo's ROOT: the
pipeline library, worker agent, validator image, Packer AMI, Terraform, configs,
Bunny edge script and docs all sit at top level here.

`forensic-wm/` is a **NESTED git submodule** of this repo — a pointer to a
pinned commit of another repository, registered in `.gitmodules` at path
`forensic-wm/`. It pins commit
`9122e60f7407a0ea394bf35a86ddeb055586ef57` of its OWN public repository
`https://github.com/mohamed17717/videoguard-forensic-watermark.git`, so a
pipeline commit transitively pins the generator source. A fresh clone needs
`git clone --recurse-submodules` (or `actions/checkout` with
`submodules: recursive`); GitHub "Download ZIP" and `git archive` do NOT
include submodule contents. Only the CI `images` job needs those files, so
only its checkout uses `submodules: recursive`. Because the URL is public
HTTPS, there is NO SSH deploy key and NO `webfactory/ssh-agent` step.
Nothing inside `forensic-wm/` was renamed or rewritten; it keeps its own
`AGENTS.md`, `.dockerignore` and `.gitignore`.

Mandatory amendments:

- Python package name is **`vg_pipeline`** (NOT `src`, NOT an import of
  `forensic-wm`). The generator is a pinned CLI/container, not a library.
- Generator image build is exactly:
  `docker build --platform=linux/amd64 -f forensic-wm/deploy/generator/Dockerfile -t <ecr>/forensic-wm-generator:hevc-v1-<sha> forensic-wm`
  (context = `forensic-wm`, so the existing `forensic-wm/.dockerignore` allowlist
  applies; the Dockerfile is NOT at `deploy/generator/.dockerignore`; the ROOT
  `.dockerignore` does not apply to this context either).
- CI workflow lives at **`.github/workflows/aws-pipeline.yml`** in THIS repo
  (GitHub only reads root-level `.github/workflows/`). Its `images` job uses
  `actions/checkout` with `submodules: recursive` to populate `forensic-wm/`;
  no SSH key and no `webfactory/ssh-agent` are needed (public HTTPS).
- The worker/validator image build context is this repo ROOT (`.`) and must
  NOT copy `forensic-wm/` (the root `.dockerignore` excludes it).
- `reports/<job_id>/manifest.json` pins together: this repo's commit
  SHA (`<sha>`), generator image digest, Shaka Packager version,
  FFmpeg version, AMI ID, ladder, KID (never key/IV), segment counts,
  per-artifact SHA-256, source SHA-256, retention, and watermark secret
  VersionId.
- `Dockerfile.api` stays at the `video-guard` repo root (the Go backend is NOT
  in this repo; see §9).

## 1. Identity, job sidecar and S3 layout

- `job_id` = UUIDv4 created by the backend at upload-session start. It is the
  ONLY correlation key across S3, SQS, webhooks and the database.
- `video_public_id` = existing `models.Video.PublicID` (UUID).
- Bucket `vg-media-<env>` in `us-east-1` (`env` = `dev` | `prod`).
- A "prefix" is a folder-like path at the start of an S3 key.

**The backend writes `jobs/<job_id>/job.json` BEFORE returning the presigned
URL.** A raw S3 event does not carry `video_public_id`, and the Axinom content
ID must equal `video_public_id.String()` because `internal/drm/token.go` puts
that value into the license token. Sidecar schema (`schema_version: 1`):

```json
{
  "schema_version": 1,
  "job_id": "uuid",
  "run_id": "uuid",
  "run_epoch": 1,
  "video_public_id": "uuid",
  "tenant_slug": "string",
  "content_id": "video_public_id.String()",
  "content_type": "video/mp4",
  "size_bytes": 1073741824,
  "retention": "archive | ttl30",
  "trace_id": "uuid",
  "created_at": "RFC3339"
}
```

`run_id` is an opaque UUID; `run_epoch` is a **monotonic uint64** assigned by
the backend under a row lock for every dispatch or requeue. `run_epoch` (NOT the
random UUID) orders webhook events across worker attempts (see §3). The worker
takes `run_epoch`/`run_id` from the `JobMessage` for a requeue/reconciliation
and from the sidecar only for a raw S3 event. The worker reads the sidecar with
`s3:GetObject` on `jobs/<job_id>/*`. If the sidecar is missing, or its
`size_bytes` does not equal the S3 event's `object.size`, fail the job to the
DLQ with `SIDECAR_INVALID`.

**Backend/Railway IAM:** the backend user needs `s3:PutObject` on
`jobs/<job_id>/*` (to write the sidecar) and on `incoming/<job_id>/*` (the
presigner's credentials must be authorised for the PUT the browser will make);
`s3:GetObject` on `jobs/<job_id>/*` (the `SidecarVideoResolver` in the webhook
handler); `s3:GetObject` on `reports/<job_id>/*` and `renditions/<job_id>/*`
plus `s3:ListBucket` scoped by an `s3:prefix` condition to those two prefixes
(reconciliation); and `sqs:SendMessage` on the jobs queue. `pricing/SECURITY.md`
§4 and `infra/iam.tf` carry the same list.

| Key | Content | Class | CDN |
|---|---|---|---|
| `jobs/<job_id>/job.json` | job sidecar | Standard | never |
| `incoming/<job_id>/source.mp4` | raw browser upload = the exact generator `--input` ("original") | Standard during job, Glacier IR after `processed=true` | never |
| `masters/<job_id>/variant_a.mp4`, `variant_b.mp4` | watermarked A/B masters | Glacier IR after success | never |
| `reports/<job_id>/config.json`, `generation_report.json`, `manifest.json`, `_LOCK`, `_DONE` | evidence + lock/completion markers | Standard | never |
| `renditions/<job_id>/init/<quality>/init.mp4` | ONE canonical, time-normalised init per quality, shared by A+B | Glacier IR from day 0 | yes |
| `renditions/<job_id>/a/<quality>/seg_NNNNN.m4s` | A variant CMAF segments | Glacier IR from day 0 | yes |
| `renditions/<job_id>/b/<quality>/seg_NNNNN.m4s` | B variant CMAF segments | Glacier IR from day 0 | yes |
| `renditions/<job_id>/audio/init.mp4`, `seg_NNNNN.m4s` | one shared audio tree | Glacier IR from day 0 | yes |
| `renditions/<job_id>/hls/master.m3u8`, `media_<quality>.m3u8`, `audio.m3u8` | canonical HLS manifests | Standard | yes (personalised by edge) |
| `renditions/<job_id>/dash/stream.mpd` | canonical DASH manifest | Standard | yes (personalised by edge) |
| `renditions/<job_id>/thumbs/thumb_NN.jpg` | 6 thumbnails | Standard | yes |
| `db-backups/` | unrelated, already exists | Deep Archive | never |

Only `renditions/*` is readable by the Bunny origin identity (`s3:GetObject`).
Thumbnails MUST live under `renditions/<job_id>/thumbs/` — a sibling `thumbs/`
prefix is unreachable through a pull zone whose origin path is `renditions/`.

A "segment" is a small (~2 s) media file. CMAF is a container format letting HLS
and DASH share the same segments. HLS uses `.m3u8` playlists; DASH uses an
`.mpd` manifest. An "init segment" holds the codec configuration a player needs
before the first media segment.

Canonical segment URLs are root-relative and contain the literal token
`__VG_VARIANT__`, e.g. `/<job_id>/__VG_VARIANT__/720p/seg_00001.m4s`. The
canonical init URL has NO variant token:
`/<job_id>/init/720p/init.mp4`. The edge replaces the token per segment and
leaves the init URL untouched.

**Retention:** `retention=archive` → outputs use `GLACIER_IR` from day 0 and the
original is tagged `processed=true` (lifecycle → Glacier IR).
`retention=ttl30` → everything stays `STANDARD`, is tagged `vg-retention=ttl30`,
and expires at 30 days; NEVER transition to Glacier (90-day minimum would
outlive the TTL) and never set `processed=true`. Every output object is tagged
`vg-retention=<retention>`. Bucket versioning is disabled (or has a
noncurrent-version expiration) so deleted TTL titles stop billing.

**Input contract (Option A, no normalisation):** v1 does NOT normalise untrusted
uploads (SECURITY.md §2). The browser upload is assumed to satisfy the
generator's production input contract; the worker's `validated` stage re-checks
with ffprobe and **fails closed** to the DLQ. Non-compliant uploads are rejected,
not converted. `pipeline.yaml` must carry the FULL contract (see §4). A future
normalisation stage, if added, must write a separate mezzanine and record both
SHA-256 values.

## 2. SQS (dispatch only)

- Queue `vg-video-jobs-<env>`, DLQ `vg-video-jobs-<env>-dlq`.
- `maxReceiveCount = 3`, visibility timeout **900 s** (not 7200 s), message
  retention 4 days, DLQ retention 14 days.
- The worker calls `sqs:ChangeMessageVisibility` every 300 s while a job runs
  (heartbeat) from a BACKGROUND supervisor thread, so a long generation/encode/
  package/upload cannot block the beat; a dead worker's message returns in
  ~15 min, not 2 h. The same thread enforces the 3600 s deadline and signals
  only the registered CHILD process groups (children are started with
  `start_new_session=True`; never signal the supervisor's own group).
- Deterministic failures (`INPUT_CONTRACT`, `CLAMAV`, `GENERATOR_EXIT_2`,
  `PACKAGING_*`, `SIDECAR_INVALID`) go to the DLQ **immediately**:
  `sqs:SendMessage` to the DLQ + `DeleteMessage`. Only transient failures use
  `maxReceiveCount=3`. Alarm on DLQ depth > 0.
- S3 event notification: `s3:ObjectCreated:Put` (NOT `*`; a Copy into
  `incoming/` must not loop), filter `prefix=incoming/`, `suffix=.mp4`. Ignore
  `s3:TestEvent`. The worker has an IAM **Deny** on `s3:PutObject` for
  `incoming/*` and never writes there.
- Queue policy: allow `s3.amazonaws.com` `sqs:SendMessage` with
  `ArnEquals aws:SourceArn = arn:aws:s3:::vg-media-<env>` and
  `StringEquals aws:SourceAccount = <account>`.
- Key parsing: `url.QueryUnescape` (S3 encodes spaces as `+`), then match
  `^incoming/([0-9a-f-]{36})/source\.mp4$`; capture group 1 is `job_id`.
  Reject anything else to the DLQ.
- Worker accepts BOTH shapes via `normalize_message()`:
  1. raw S3 event `{"Records":[{"s3":{"object":{"key":...,"size":...}}}]}`;
  2. canonical `JobMessage` (backend requeue / reconciliation), which is the
     sidecar schema plus `"reason": "s3 | requeue"`. Every requeue generates a
     NEW `run_id` and a strictly greater `run_epoch`, which supersedes earlier
     runs. The worker MUST take these from the `JobMessage`, not the sidecar.
- **Conditional lock, not a TOCTOU `_DONE` check:** before any work,
  `PutObject reports/<job_id>/_LOCK` with `If-None-Match: *`, body
  `{instance_id, attempt, expires_at}`. On 412, read the lock body **and ETag
  from ONE GetObject** (a separate HEAD could observe a newer lock); if
  `expires_at` is in the future the message is left VISIBLE for a later retry
  (do NOT delete it: a busy lock is not proof another worker will finish), and
  the worker terminates. If expired, take over with a conditional write on the
  ETag from that same GET. Renew the lock every 300 s; abort if the ETag
  changes.
- A failed or DLQ'd attempt expires its own `_LOCK` with a conditional
  `PutObject` (`{"expires_at": 0}`, `If-Match: <our ETag>`) so the next delivery
  can acquire it. Leaving a live 1200 s lock behind while SQS redelivers after
  900 s used to make the next worker see "busy" and delete the job.
- `ReceiveMessage` MUST request `AttributeNames=["ApproximateReceiveCount"]`;
  otherwise `attempt` always defaults to 1 and the final-attempt `failed`
  webhook is never emitted.
- On a redelivery, re-POST `done` **only when the stored `_DONE` marker's
  `run_epoch` equals the message's `run_epoch`**. An older marker from a
  superseded run must NOT short-circuit an explicit requeue.
- The worker IAM policy includes `s3:ListBucket` on the bucket scoped by
  `s3:prefix` to `reports/*` and `renditions/*`. Without it, `HeadObject` on a
  missing marker returns 403 (not 404).

**ASG:** min 0 / max 3, `g6.xlarge`. Scale-out uses TWO scale-out-only
policies: (a) a CloudWatch alarm on `ApproximateNumberOfMessagesVisible >= 1`
that triggers a StepScaling `ChangeInCapacity +1` (the explicit zero→one
trigger — target tracking on a raw value of 1 does not scale), and (b) a
TargetTracking policy on the capacity-aware metric math
`IF(inservice > 0, visible / inservice, visible)` with target 1 and
`DisableScaleIn: true`. There is NO scale-in policy. A received message is
invisible, so a scale-in on that metric could terminate a busy worker. Each
worker processes exactly one message (`MaxNumberOfMessages=1`) and terminates
itself; that is the ONLY scale-in path. Set
`InstanceInitiatedShutdownBehavior=terminate`, `EstimatedInstanceWarmup=300`.
Launch template: 100 GiB gp3 encrypted, delete-on-termination; fail with
`DISK_FULL` if free space < 2.5× source + 20 GiB. The launch-template
`VG_ASG_NAME` comes from a LOCAL value (`vg-worker-<env>`), never from
`aws_autoscaling_group.worker.name` (that creates a Terraform dependency cycle).

## 3. Progress webhooks

One short POST per stage from worker to Railway. Railway verifies HMAC/timestamp,
performs a **synchronous** conditional upsert into `pipeline_webhook_events`
(`ON CONFLICT ... DO UPDATE ... WHERE` the incoming `(run_epoch, attempt, seq)`
is greater — see §9), then returns 200. The DB write is NOT deferred behind the
response (immediate 200 + fire-and-forget DB write is at-most-once). Plain
`ON CONFLICT DO NOTHING` is forbidden: it would drop a superseding run's events.

Headers:

```
Content-Type: application/json
X-VG-Timestamp: <unix seconds>
X-VG-Signature: sha256=<hex>
```

Signature = `HMAC_SHA256(webhook_secret, f"{ts}.{job_id}.{stage}.{seq}.{raw_body}")`.
Reject when `abs(now - ts) > 300` s. Each retry signs with a FRESH timestamp.

Body (`schema_version: 1`):

```json
{
  "schema_version": 1,
  "job_id": "uuid",
  "run_id": "uuid",
  "run_epoch": 1,
  "stage": "rendition_ready",
  "seq": 7,
  "attempt": 1,
  "timestamp": 1730000000,
  "qualifier": "720p",
  "retryable": false,
  "payload": { }
}
```

- **Order by `(run_epoch, attempt, seq)`**, never by the random `run_id` and
  never by `seq` alone. `run_epoch` is a monotonic uint64 assigned by the
  backend; `seq` restarts at 1 on each worker attempt (the worker is ephemeral,
  so a retry cannot know the previous counter). A new `run_epoch` supersedes all
  earlier runs. Store an event if its `(run_epoch, attempt, seq)` tuple is
  greater than the last stored tuple — use a conditional upsert (see §9), NEVER
  `ON CONFLICT DO NOTHING`, which would silently drop a new run's repeated
  `validated`/`masters_ready`/`rendition_ready` events. `done` from the highest
  `(run_epoch, attempt, seq)` is always accepted and is **terminal**: a late
  `rendition_ready` must never clear it. A re-POST of the same `done` is
  idempotent via the unique key.
- **Idempotency key = `job_id + ":" + stage + ":" + qualifier`**, backed by ONE
  unique index `pipeline_webhook_events (job_id, stage, qualifier)`. The
  qualifier is required because `rendition_ready` fires once per quality. A
  superseding run updates the existing row via the conditional upsert in §9; it
  does NOT create a second row.
- `attempt` = SQS `ApproximateReceiveCount`.
- `failed` is sent ONLY on the final attempt, with
  `qualifier = stage_at_failure` and `retryable:false`. A transient failure must
  not permanently fail the video.
- `rendition_ready` is sent only AFTER a successful `PutObject` (and a
  `HeadObject`/ETag check) for both a+b of that quality.
- `_DONE` is written AFTER the bounded `done` POST attempt, not before. It
  contains `{job_id, run_epoch, completed_at, manifest_key, webhooks_ok,
  attempt, done_payload}`; `done_payload` is the authoritative completion
  metadata used by reconciliation.
- **Atomic apply:** the conditional event upsert, the terminal `done`/`failed`
  application to the Video row, and the quota confirm/cancel callback are ONE
  DB transaction. If any step fails, roll back and return 5xx (never a 200), so
  the event can be retried and reconciliation can heal. A terminal event whose
  `run_epoch` is LOWER than the stored terminal epoch is rejected; a newer
  `failed` is not suppressed by an older `done`.
- Retry 3× with 1 s / 2 s / 4 s backoff, fire-and-forget: a slow Railway must
  never hold the GPU box open. Flush the final webhook with a bounded 5 s wait
  before termination.
- Reconciliation: if all webhooks fail, files still land in S3; the backend
  rebuilds status from `reports/` + `renditions/`, preferring the
  `done_payload` stored in `_DONE` (the provenance `manifest.json` has no
  duration/storage accounting). Before requeueing a stuck video it checks the
  live `_LOCK` and skips while it is unexpired; a successful requeue touches the
  video row so the same job is not requeued on every tick.

| Stage | qualifier | payload (minimum) |
|---|---|---|
| `validated` | `""` | probe summary, caps verdict |
| `masters_ready` | `""` | master keys, config/report keys, sha256 |
| `rendition_ready` | quality | quality, bytes, keys for a+b |
| `packaged` | `""` | hls/dash keys, segment counts, aligned-timeline verdict |
| `done` | `""` | qualities[], hls_key, dash_key, thumbnail keys, duration_seconds, storage_bytes, sha256, watermark_secret_version, retention |
| `failed` | stage_at_failure | error_code, message, stage_at_failure |

## 4. Config files (single source of truth)

`configs/pipeline.yaml`:

```yaml
schema_version: 1
profile: hevc-v1
segment_duration_sec: 2
gop_seconds: 2
thumbnail_count: 6
drm:
  scheme: cbcs
  systems: [Widevine, PlayReady, FairPlay]
  clear_lead: 0
ladder:
  - {name: 720p, width: 1280, height: 720, target_kbps: 2800, maxrate_kbps: 3800, bufsize_kbps: 7600, nvenc_cq: 23}
  - {name: 480p, width: 854,  height: 480, target_kbps: 1400, maxrate_kbps: 2200, bufsize_kbps: 4400, nvenc_cq: 23}
  - {name: 360p, width: 640,  height: 360, target_kbps: 800,  maxrate_kbps: 1600, bufsize_kbps: 3200, nvenc_cq: 24}
  - {name: 240p, width: 426,  height: 240, target_kbps: 600,  maxrate_kbps: 1200, bufsize_kbps: 2400, nvenc_cq: 25}
input_contract:
  require_cfr: true
  reject_vfr: true
  reject_hdr: true
  max_bit_depth: 8
  allowed_codecs: [h264, hevc]
  allowed_pix_fmts: [yuv420p, nv12]
  allowed_color_transfer: [bt709, smpte170m]
  allowed_color_primaries: [bt709, smpte170m]
  allowed_color_range: [tv]
  require_even_dimensions: true
  allowed_audio_codecs: [aac]
  max_audio_streams: 1
  allowed_containers: [mov, mp4, m4a, 3gp, 3g2, mj2]
caps:
  max_bytes: 2147483648
  max_duration_sec: 3600
  max_width: 3840
  max_height: 2160
  max_frames: 216000
  max_streams: 8
```

Ladder is 720/480/360/240p (resolves PRICING.md's "ladder mismatch"; no 1080p).
Skip qualities whose width/height exceed the probed source; `done.qualities[]`
and the master playlist contain only produced qualities.

**Output codec:** v1 uses H.264 (`h264_nvenc` on the GPU, `libx264` CPU fallback)
for device compatibility. Per rendition:

```
-rc vbr -cq <nvenc_cq> -b:v <target_kbps>k -maxrate <maxrate_kbps>k
-bufsize <bufsize_kbps>k -g <round(fps*2)> -keyint_min <round(fps*2)>
-sc_threshold 0 -force_key_frames expr:gte(t,n_forced*2)
-fps_mode cfr -r <fps> -pix_fmt yuv420p
```

Never pass both `-crf` and `-cq`. If the source has no audio, omit the audio
tree and the `EXT-X-MEDIA` group. Do not copy the old `ffprobe.go`
`max(parseFPS,30)` bug or the old `ffmpeg.go` 100 s timeout; the worker enforces
the 3600 s deadline itself.

`configs/aws.worker.yaml`: runtime settings (prefixes,
deadlines, image names, Secrets Manager ARN placeholders, packager/ffprobe
binary paths, work dir). No global `storage_class_renditions` — storage class is
chosen per job from `retention`.

## 5. Worker environment and secrets

```
VG_AWS_REGION, VG_S3_BUCKET, VG_SQS_QUEUE_URL, VG_SQS_DLQ_URL,
VG_WEBHOOK_URL, VG_WEBHOOK_SECRET_ARN,
VG_AXINOM_SECRET_ARN, VG_WATERMARK_SECRET_ARN,
VG_ASG_NAME, VG_JOB_DEADLINE_SEC=3600,
VG_CONFIG_PATH=/etc/vg/pipeline.yaml, VG_WORK_DIR=/work,
VG_GENERATOR_IMAGE, VG_VALIDATOR_IMAGE,
VG_PACKAGER_BIN=/usr/local/bin/packager,
VG_FFPROBE_BIN=/usr/local/bin/ffprobe,
VG_FFMPEG_BIN=/usr/local/bin/ffmpeg, VG_LOG_LEVEL
```

The launch-template `user_data` is a JSON object read at boot by
`deploy/worker/bootstrap-env.sh` over IMDSv2; it writes `/etc/vg/worker.env`
and `entrypoint.sh` sources it. `VG_CONFIG_PATH` is the MEDIA pipeline config
(`/etc/vg/pipeline.yaml`, with `profile`/`ladder`/`caps`); the worker runtime
config is passed separately as `--config /etc/vg/worker.yaml`. The host
`vgworker` service user uses UID/GID 10001 — the SAME uid/gid as the
generator/validator containers — so the 0400 secret file and the `/work`
output tree need no root chown.

**Pinned FFmpeg on the AMI:** the generator image is GPU-only and intentionally
has NO `libx264`, so the CPU `libx264` fallback cannot run there. The AMI (or
worker image) ships a SECOND pinned FFmpeg build (same pinned FFmpeg source, with
`libx264` + `h264_nvenc` enabled) at `VG_FFMPEG_BIN`; the ladder and audio
extraction use that binary, not the generator image. If `h264_nvenc` is
unavailable, fall back to `libx264` from that same binary; if neither is present,
fail closed with `ENCODER_UNAVAILABLE`.

Secret JSON shapes:

- watermark: `{"secret":"..."}`
- Axinom: `{"signing_key_hex":"...","signing_iv_hex":"...","endpoint":"...","provider_name":"...","scheme":"cbcs"}`
- webhook: `{"secret":"..."}`

Fetched with `secretsmanager:GetSecretValue`, held in process memory only. For
the generator, write the watermark secret to a 64 KiB **tmpfs** file
`/run/vg-secrets/watermark` (mode 0400, owner UID/GID 10001), bind-mount it
read-only at `/run/secrets/watermark`, pass
`-e WATERMARK_SECRET_FILE=/run/secrets/watermark`, and unlink/unmount after the
container exits. Never write the secret to `/work`, S3, the image,
`docker run -e`, command-line arguments, `config.json`, `generation_report.json`
or logs. Record the Secrets Manager `VersionId` in `manifest.json` and `done`.

## 6. Packaging (D2 — REPLACED after DeepSeek DISAGREE)

**ONE Shaka Packager run with 9 stream descriptors:** 8 video
(`a/720p`, `b/720p`, `a/480p`, `b/480p`, `a/360p`, `b/360p`, `a/240p`,
`b/240p`) and 1 audio (`audio/`), all with the SAME
`--keys=key_id=<KID>:key=<KEY>` and `--iv=<IV>`. Discard Shaka's generated
master playlist and MPD (write them to a temp path); `artifacts.py` writes the
canonical manifests so A/B is never exposed as 8 variants in a published
master.

**Inputs:** the 8 video descriptors use the PRE-ENCRYPTED H.264 ladder outputs
(`encoded/<variant>/<quality>.mp4`), NEVER the HEVC masters. Segment files are
sorted by their NUMERIC index (Shaka emits unpadded `seg_<n>.m4s`), then
published as padded `seg_00001.m4s`.

Why: two separate runs cannot produce byte-identical raw `init.mp4` because
Shaka's MP4 muxer writes wall-clock `creation_time`/`modification_time` into
`mvhd`/`tkhd`/`mdhd`. A raw byte-equality gate would fail every job, and
"initialise from segment 1's variant" is unsound (a player loads one init per
Representation).

**Init canonicalisation:** rewrite `creation_time`/`modification_time` to 0 in
every `init.mp4` (`mvhd`, `tkhd`, `mdhd`) using a small MP4 box walker. Upload
ONE canonical `init.mp4` per quality to `renditions/<job>/init/<quality>/`.
Both A and B segments reference that same init URL (no variant token).

**Aligned-timeline gate** (all three must pass, else fail the job with
`PACKAGING_INIT_MISMATCH`; NO fallback):

1. equal segment count per quality for a and b;
2. equal `tfdt` `baseMediaDecodeTime` for each segment index;
3. byte-identical **normalised** A/B `init.mp4` per quality.

**Real timelines:** the canonical manifests use the per-segment `#EXTINF`
durations from Shaka's own media playlists (read before those playlists are
discarded); the final partial segment and the audio tree keep their own
duration/count. The DASH MPD uses a `SegmentTimeline` when real durations are
available instead of a fixed segment duration.

**HLS key signalling:** because Shaka's playlists are discarded, the canonical
media playlists MUST carry
`#EXT-X-KEY:METHOD=SAMPLE-AES,URI="<SKD_URI>",KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"`.
DASH already carries `ContentProtection`.

**Audio** is packaged exactly once, in the same run; the B descriptors are
video-only. Shaka flags (one run):

```
--segment_duration 2 --generate_static_live_mpd
--protection_systems=Widevine,PlayReady,FairPlay
--clear_lead=0 --protection_scheme=cbcs
--enable_raw_key_encryption --keys=key_id=<KID>:key=<KEY> --iv=<IV>
--pssh=<PSSH> --hls_key_uri=<SKD_URI>
--hls_master_playlist_output <tmp> --mpd_output <tmp>
```

Keep `--pssh` / `--protection_systems` only after a live Shaka test proves the
MPD/HLS PSSH is not duplicated (deferred gate).

**Axinom port (Go → Python):**

- `protection_scheme = "CBCS"`, NOT `"CENC"`. The old Go `cenc.go` hard-codes
  `"CENC"` while Shaka uses `cbcs`; `docs/shaka-6001-iphone-investigation.md`
  documents this inconsistency.
- `content_id = video_public_id.String()` (from the sidecar), matching
  `internal/drm/token.go`.
- Assert `len(tracks) == 1`; fail otherwise instead of silently using `tracks[0]`.
- NEVER log the Axinom request or response (the response contains key/IV).
- Preserve exactly: request JSON bytes, SHA-1 of `json.MarshalIndent(..., "", "  ")`,
  AES-256-CBC/PKCS#7 signature over the 20-byte digest padded to 32 bytes with
  `0x0C`, base64 envelope `{request, signature, signer}`, PSSH v0 box construction
  (size/type/version+flags/system_id/data_size/data), KID/KEY/IV hex and SKD URI.
- Golden-vector tests generated from the existing Go implementation must cover
  all of the above.

**Storage accounting:** `done.storage_bytes` = the published delivery objects +
the original upload + both retained masters (the quota confirmation uses this
total).

## 7. Delivery and A/B decision

- Backend issues a short-lived signed playback token passed as `?vg_token=` on
  the manifest URL. v1 uses 3600 s (or 60 s with a refresh endpoint). Claims:
  `job_id`, `video_public_id`, `session_id`, `seed`, `exp`. HMAC-signed, verified
  at the edge.
- Backend logs `session_id → seed` in `playback_variant_assignments` at issuance
  (leak attribution cannot be rebuilt later).
- Exact pattern: `digest = HMAC_SHA256(seed.encode(), f"vg-ab-{n}".encode())`;
  `variant = "a" if digest[0] & 1 == 0 else "b"`. Golden vectors in both Python
  and Deno.
- **HLS:** per-segment substitution — the edge replaces `__VG_VARIANT__` in each
  media-playlist segment URI. `#EXT-X-MAP` points to the shared canonical init
  (no variant token).
- **DASH:** one variant per session, chosen from `pattern(seed, 0)`; rewrite the
  `SegmentTemplate` `initialization`/`media` paths to that variant. Do NOT
  generate a per-segment `SegmentList` at the edge (1800 × 4 = 7200 entries).
  Per-segment DASH is a later, gated feature.
- The edge handles only `*.m3u8` and `*.mpd`; segments and thumbnails pass
  through. EVERY origin fetch (canonical manifests AND pass-through segments)
  targets a **distinct internal origin hostname** (`originHost + path`, NOT the
  public `req.url`, which would re-enter the script) plus an AUTHENTICATED
  `X-VG-Origin-Fetch` header whose value is
  `hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path))`. The edge recomputes
  and constant-time-compares that value (and checks the request hostname)
  before honouring the bypass; a plain `1`, a forged value or a wrong hostname
  returns 403 and never falls through to token auth. A MISSING `vg_token` is
  still rejected. The verified token's `job_id` must match the requested path
  prefix (`/<job_id>/...`), or the request is 403. No S3 credentials in the
  edge script.
- **Fail closed:** `VG_EDGE_PARAMS` MUST supply BOTH `tokenSecret` (equal to
  the backend's `PLAYBACK_EDGE_TOKEN_SECRET`) and `originHost`; an empty either
  returns 500 on every request. CI's `edge` job
  (`.github/workflows/aws-pipeline.yml`) is the SOLE owner of the edge script:
  it reads GitHub `secrets.EDGE_TOKEN_SECRET` / `vars.EDGE_ORIGIN_HOST` and
  injects `VG_EDGE_PARAMS`; Terraform deliberately does NOT manage the edge
  script (`infra/bunny.tf` owns only the pull zone + Cloudflare DNS). The exact
  Bunny API field name for script parameters and the numeric script Id handling
  are §11 deferred gates.
- Manifest responses: `Cache-Control: private, no-store`, pull-zone cache bypass
  for `*.m3u8`/`*.mpd`, plus CORS (`Access-Control-Allow-Origin`, methods,
  headers incl. `Range`, exposed `Content-Length`/`Content-Range`/`ETag`,
  `Vary: Origin`). Segments: `Cache-Control: public, max-age=31536000,
  immutable`.
- The CI `edge` job also ATTACHES the published script to the playback pull
  zone (`EdgeScriptId` = the numeric script Id); without it the zone serves the
  canonical `__VG_VARIANT__` manifests. The exact Bunny endpoint/field is a §11
  deferred gate.
- Playback API returns `hls_manifest_path` (NOT `hls_playlist_path`) and
  `dash_manifest_path`; the SDK already prefers `hls_manifest_path`. Do not
  modify the `sdk/` submodule in the `video-guard` repo.

## 8. Worker lifecycle

0. Boot: `bootstrap-env.sh` reads the launch-template user_data over IMDSv2 and
   writes `/etc/vg/worker.env`; `entrypoint.sh` sources it and runs
   `aws ecr get-login-password | docker login` (instance role) before the agent.
1. Receive exactly ONE SQS message, normalise, read `jobs/<job_id>/job.json`,
   acquire `_LOCK`, start the 3600 s deadline and the 300 s background
   heartbeat thread.
2. `validated`: download to read-only `/work/input`, run ffprobe + ClamAV in the
   network-less `validator` container with the full input contract and hard caps.
3. `masters_ready`: run the `generator` container (`--gpus all`,
   `--network=none`, `--read-only`, `--cap-drop ALL`,
   `--security-opt no-new-privileges`, non-root UID 10001, tmpfs secret 0400,
   `--backend cuda`). With `--read-only` the image's `/work/output` is part of
   the read-only rootfs, so add a writable per-job PARENT mount
   `-v /work/generator-output:/work/output:rw` owned by UID/GID 10001 and pass
   `--output-dir /work/output/generated`: production explicitly REJECTS an
   existing output directory, so the child must be NEW. Plus
   `--tmpfs /tmp:rw,noexec,nosuid,size=512m`. Keep `/work/input` and the secret
   `:ro`. Verify the 4 atomic outputs in the child, publish reports + masters.
4. ladder: 2 variants × produced qualities + 6 thumbnails; upload each with the
   storage class chosen from `retention`; webhook per quality.
5. `packaged`: one Axinom key request, one Shaka run, init canonicalisation,
   aligned-timeline gate, canonical manifests, upload.
6. `done`: tag the original (`processed=true` for archive only), POST `done`,
   write `_DONE`, delete the SQS message.
7. Terminate: `autoscaling:TerminateInstanceInAutoScalingGroup` on its own
   IMDSv2 instance-id with `--should-decrement-desired-capacity`. Retry 3× on
   `ScalingActivityInProgress`/`ValidationError`, then `shutdown -h now`
   (launch template set to terminate). NEVER use
   `--no-should-decrement-desired-capacity` (launches a replacement). If the ASG
   already set desired to 0, let it terminate. Do not poll a second message.
   The deadline signaler targets only the registered CHILD process groups
   (`start_new_session=True`); it NEVER signals the supervisor's own group.
8. On ANY failure: `failed` webhook (final attempt only), deterministic → DLQ
   immediately, transient → leave message, terminate. No partial published
   output, no reused instance. Supervisor SIGKILLs 30–60 s after SIGTERM on
   deadline. Log instance ID, AMI ID, image digest and job ID together.

**IAM (worker role):** `s3:GetObject` on `incoming/<job>/*`, `jobs/<job>/*`,
`reports/<job>/*` (to read `_LOCK` on a 412 and `_DONE` on redelivery), and
`renditions/<job>/*` (`HeadObject` for the a+b upload check before
`rendition_ready`); `s3:PutObject` on `masters/<job>/*`, `renditions/<job>/*`,
`reports/<job>/*`; `s3:PutObjectTagging` on `masters/<job>/*`,
`renditions/<job>/*`, `reports/<job>/*` AND `incoming/<job>/*` (tag
`processed=true` for archive and `vg-retention=ttl30` for TTL); explicit Deny
on `s3:PutObject` for `incoming/*` ONLY (tagging remains allowed);
`sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`,
`GetQueueAttributes` on the jobs queue; `sqs:SendMessage` on the **DLQ ARN**
(for deterministic failure routing); `secretsmanager:GetSecretValue` on three
ARNs; `autoscaling:TerminateInstanceInAutoScalingGroup`;
`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
`ecr:GetDownloadUrlForLayer`; `logs:CreateLogStream`, `logs:PutLogEvents`. Run
the agent as a host systemd service, NOT a container with `/var/run/docker.sock`.

**Network honesty:** a security group cannot allowlist hostnames and Railway
does not publish a stable IP list. Either use VPC interface endpoints for
SQS/Secrets Manager/ECR/CloudWatch + an S3 gateway endpoint and a documented
Railway egress CIDR, or allow 443 to `0.0.0.0/0` and state that the ephemeral
instance — not the SG — is the boundary. Do NOT claim "no 0.0.0.0/0 egress"
unless endpoints are actually implemented. No inbound rules.

## 9. Go control plane — ORDERED, BUILD-GREEN MIGRATION

**Repo split:** the Go control plane lives in the SEPARATE `video-guard`
repository — this repo contains none of it. Its CI is
`video-guard/.github/workflows/ci.yml`, and every
`go build ./... && go test ./...` below runs THERE (this repo's CI has no Go
toolchain). The steps below are the migration order for `video-guard`.

The delete list cannot be applied in one shot: `cmd/api/main.go` imports
`internal/bunny/controllers` and `internal/rabbitmq`, `config.Load()` requires
both YAML files, `Dockerfile.api` copies them, and `internal/bunny/controllers`
imports `internal/bunny/storage` and `internal/transcode`. Many handlers and
tests reference those packages. Follow this order and run
`go build ./... && go test ./...` (in the `video-guard` repo) after every step:

1. ADD `internal/awscontrol/**`, new models, routes and tests while the old code
   still compiles. Add `internal/drm/token.go` (moved copy of
   `internal/transcode/drm_token.go`, keeping `GetAxinomDRMToken`). Repoint the
   three callers (`playback_session_handler.go`, `dashboard_video_handler.go`,
   `playback_drm_service.go`).
2. REMOVE `cmd/worker`, `Dockerfile.worker`, `internal/rabbitmq/**`,
   `configs/rabbitmq.yaml`, `configs/video.processing.yaml`, the RabbitMQ service
   from `docker-compose*.yml`, the RabbitMQ init in `cmd/api/main.go`, and
   `api.RouterDependencies.Rabbit`. BEFORE removing the packages, rewrite or
   remove these remaining consumers: `internal/api/router.go` (drop the `Rabbit`
   field, `handlers.Initialize`, `NewAdminJobService`, `NewSystemHealthService`,
   `NewBunnyVideoStatusService`, `NewBunnyStatusPoller`, `BunnyWebhookSecret`,
   `NewBunnyStreamWebhookSignatureMiddleware`),
   `internal/api/services/admin_job_service.go`,
   `internal/api/services/system_health_service.go`,
   `internal/api/handlers/admin_job_handler.go`,
   `internal/api/handlers/system_runtime.go`,
   `internal/api/handlers/system_upload_handler.go`,
   `internal/api/handlers/system_handler.go` (drop `videoStatuses`),
   `internal/api/services/admin_dashboard_requeue_service.go` (imports
   `internal/rabbitmq` and `internal/rabbitmq/workers`),
   `internal/api/services/admin_dashboard_requeue_service_test.go`,
   `internal/api/handlers/admin_dashboard_requeue_handler.go`, and
   `NewAdminVideoRequeueService` in `router.go`/`routes_contract_test.go`, plus
   the tests `constructor_requirements_test.go`, `billing_integration_test.go`,
   `media_service_test.go`, `bunny_video_status_service_test.go`,
   `routes_contract_test.go`. `AdminVideoRequeueService` must publish a new
   `JobMessage` via `internal/awscontrol/sqs.go` with a new `run_epoch`/`run_id`
   via `WriteJobSidecar`, or the admin requeue route, handler, service and test
   must be removed.
3. REMOVE Bunny Stream: `internal/bunny/stream/**`, `internal/bunny/storage/**`,
   `internal/bunny/controllers/**` (or rewrite every caller),
   `bunny_video_status_service.go`, the Bunny Stream webhook handler/poller and
   `/bunny-stream-webhook` route, and the Bunny API usage in
   `video_library_service.go`, `thumbnail_service.go`,
   `admin_system_service.go`, `cmd/api/main.go`, `media_url_signing.go`,
   `internal/api/handlers/dashboard_video_handler.go` (drop `bunnyStream`,
   `NewBunnyStreamClient`, `bunny_guid`, `poller.StartPolling`), and
   `internal/api/routes/routes.go`.
4. Remove the YAML `COPY` lines from `Dockerfile.api`; refactor
   `internal/config.Load()` so it does not require the deleted YAMLs and does
   not require `AXINOM_CENC_*` on the API (the API still needs the Axinom
   communication key for the DRM token).
5. Final `go build ./... && go test ./...` (in the `video-guard` repo).

**New/changed Go surface:**

- `internal/awscontrol/presign.go`: `PresignPut(ctx, job JobSidecar) →
  (url, headers, expiresAt)`, where `JobSidecar` is the §1 schema struct. It
  writes `jobs/<job_id>/job.json` inside (or a separate
  `WriteJobSidecar(ctx, job)` is called from `StartUploadSession` before
  `PresignPut`). `TraceID` is generated once and used in both the sidecar and
  the presign response. Set `ContentLength: sizeBytes`
  (> 0 or the SDK drops `Content-Type` from `X-Amz-SignedHeaders`),
  `ContentType: "video/mp4"`, do NOT set `StorageClass` (STANDARD is the
  bucket default; signing it adds a CORS preflight for no gain). Return only
  `headers: {"Content-Type":"video/mp4"}` — browsers set `Content-Length`
  themselves and JS cannot. Test that `X-Amz-SignedHeaders` contains
  `content-length;content-type;host` and NOT `x-amz-storage-class`. The uploader
  must PUT a `Blob`/`File` of exactly `sizeBytes` (a streaming body uses chunked
  encoding and breaks the signature).
- Also write `jobs/<job_id>/job.json` before returning the presigned URL.
- `internal/awscontrol/webhook_sign.go`: `Sign` / `Verify` with the `seq` HMAC
  input from §3.
- `internal/awscontrol/webhook_handler.go`: ONE DB transaction for the
  idempotent upsert + terminal apply + quota confirm/cancel, then 200; a failure
  rolls back and returns 5xx. `done` is terminal for its `run_epoch`; a terminal
  event with a lower `run_epoch` than the stored terminal epoch is rejected.
- `internal/awscontrol/reconcile.go`, `sqs.go`, `upload_session.go`,
  `variant_assignment.go`.
- Models: `Video` gains `JobID`, `PipelineStatus`, `HLSManifestPath`,
  `DASHManifestPath`, `ThumbnailURL`, `StorageBytes`, `DurationSeconds`,
  `ErrorCode`, `Retention`, `TerminalEpoch` (keep old Bunny columns nullable
  until migration).
  `PipelineWebhookEvent`: snake_case columns `run_id uuid`, `run_epoch bigint`,
  `attempt int`, `seq int`, `payload jsonb`, `received_at`, plus `job_id`,
  `stage`, `qualifier`; ONE unique index `(job_id, stage, qualifier)` — the same
  key as §3's idempotency key. Use this exact conditional upsert (column names
  MUST be snake_case; CamelCase identifiers are not PostgreSQL columns):
  `INSERT ... ON CONFLICT (job_id, stage, qualifier) DO UPDATE SET
  run_id = EXCLUDED.run_id, run_epoch = EXCLUDED.run_epoch,
  attempt = EXCLUDED.attempt, seq = EXCLUDED.seq, payload = EXCLUDED.payload,
  received_at = EXCLUDED.received_at WHERE (EXCLUDED.run_epoch,
  EXCLUDED.attempt, EXCLUDED.seq) > (pipeline_webhook_events.run_epoch,
  pipeline_webhook_events.attempt, pipeline_webhook_events.seq)`.
  Do NOT declare a second unique `(job_id, run_epoch, stage, qualifier)` index —
  the conflict target must match the declared unique index or PostgreSQL raises
  42P10. `done` is terminal for the highest tuple.
  `PlaybackVariantAssignment`: unique `SessionID`, fields `JobID`,
  `VideoPublicID`, `Seed`, `CreatedAt`. Register both in `db.RunMigrations`.
- `StartUploadSession` returns
  `{video_public_id, job_id, upload:{method:"PUT", url, headers:{"Content-Type":"video/mp4"}, expires_at}}`;
  keep the existing quota precheck/reservation. The dashboard MUST branch on
  this `upload` descriptor and PUT the file to S3 with XHR progress; the legacy
  Bunny TUS path remains only as a fallback when `upload` is absent.
- Bucket CORS: `AllowedMethods=[PUT]`, `AllowedHeaders=[content-type]`,
  `ExposeHeaders=[ETag]`.
- Bucket hardening: Block Public Access on, SSE-S3 or SSE-KMS,
  `BucketOwnerEnforced`, deny `aws:SecureTransport=false`, no ACLs.

## 10. Engineering standards

- Python 3.12, type hints everywhere, pydantic v2 contracts, structured JSON
  logging, atomic outputs, no secrets in tracked files/logs, tests for every
  module including failure paths. Python package `vg_pipeline`.
- Go: existing style; `go build ./... && go test ./...` must pass.
- Terraform: HCL, locally parsed with `python-hcl2` (no terraform binary here);
  `terraform validate` runs in CI. Packer: `packer validate` in CI.
- Bunny edge: TypeScript + Deno unit tests.
- Tests required: S3 event normalisation (URL-encoded keys, wrong suffix,
  missing sidecar, duplicate delivery), presign `X-Amz-SignedHeaders`,
  HMAC/verify, idempotency/terminal state, Axinom golden vectors, `pattern`
  golden vector, canonical manifest golden files, init canonicalisation,
  Shaka smoke test with synthetic 2 s segments, `python-hcl2` parse, Deno edge
  unit tests, all failure paths.
- CloudWatch alarms: job duration, non-zero exits, DLQ depth, webhook failure
  rate, unusual worker egress. ECR scanning + monthly rebuild/re-pin schedule
  belong to the later hardening phase.
- CI: THIS repo's workflow is `.github/workflows/aws-pipeline.yml` (pytest +
  Deno + image build/push + `packer validate` + `terraform validate`/apply).
  The Go backend has its own CI in `video-guard/.github/workflows/ci.yml`;
  `go build ./... && go test ./...` runs THERE, not here. Branch is `main`.
- No commits, no pushes. All changes stay uncommitted in this repo
  for the owner to review and push.
- Simple English in all docs; explain technical terms on first use.

## 11. DEFERRED GATES — must NOT be claimed as done locally

No AWS credentials, no terraform/packer/aws CLI, no g6.xlarge/L4 GPU, no
Shaka binary, no Axinom credentials, no real Bunny edge runtime in this
environment.

- **AWS:** S3 event → SQS queue policy/shape; presigned PUT from a real browser
  with CORS and exact `Content-Length`; conditional `_LOCK`; ASG scale-out and
  self-termination; DLQ redrive; Glacier IR billing / 128 KiB minimum / 90-day
  minimum; ECR, Secrets Manager, CloudWatch; IAM least privilege; Bunny origin
  signing.
- **GPU:** `make gpu-acceptance GPU_BACKEND=cuda` on a real `g6.xlarge`; NVENC
  ladder; real 1-hour packaging; init canonicalisation against the real Shaka
  muxer; Widevine/PlayReady/FairPlay playback; 4K/HDR/VFR handling.
- **Terraform/Packer:** real `terraform plan`/`apply` and `packer build` in CI
  or a real account. `terraform validate` and `packer validate` (both
  credential-free) ARE CI gates in this repo's workflow — but they were never
  run in the implementation session, because there is no terraform/packer
  binary in this environment; locally the files are parse-only with
  `python-hcl2`.
- **Shaka:** not installed locally; synthetic smoke test only. Reading real
  per-segment `#EXTINF` durations and the numeric `seg_<n>` ordering from a
  real Shaka run is a gate.
- **Axinom:** golden-vector tests only; live key request / license issuance /
  FairPlay SPC-CKC are gates.
- **Bunny Edge/CDN:** Deno unit tests, the `deno bundle` →
  `edge/bunny/deploy/entry.js` build, and the CI `edge` job's ownership of the
  script (create + `VG_EDGE_PARAMS` + publish, reading GitHub
  `secrets.EDGE_TOKEN_SECRET` / `vars.EDGE_ORIGIN_HOST`) are written and
  structurally tested locally; the real edge runtime, the exact Bunny API
  field name for script parameters (`EdgeScriptParams`) AND the numeric
  script Id handling (list-by-name → capture `.Id` from the create response →
  publish to `/compute/script/<Id>/publish`, never by name),
  `VG_EDGE_PARAMS` injection, script deployment, origin signing,
  distinct-origin-host recursion semantics, cache-bypass rules, CORS field
  names (`EnableCORS`/`CorsAllowedOrigins`), the pull-zone `EdgeScriptId`
  attach endpoint/verb, and HLS master token-propagation behaviour are gates.
- **ClamAV:** validator image + EICAR test is a gate.
- **S3 object purge on video deletion** (no design or IAM yet) is a follow-up,
  not something claimed fixed locally.
- **S3→SQS delivery and every other AWS runtime behaviour** must be re-verified
  on a real account even though the Terraform parses locally.
- **Toolchain note:** `python3` is 3.11.8; use `pyenv` 3.12.0
  (`~/.pyenv/versions/3.12.0/bin/python3.12`) with pytest/pyyaml/pydantic/boto3/
  python-hcl2. Host FFmpeg is 9.0.2 while the generator image pins 7.1.5 — never
  use host FFmpeg to certify the image.
