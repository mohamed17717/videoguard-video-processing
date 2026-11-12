# infra/ — Terraform for the AWS pipeline

Everything in this directory provisions the AWS side of the video pipeline
described in `../CONTRACT.md` (the agreed baseline). Written in simple
English; technical terms are explained on first use.

## What is here

| File | What it creates |
|---|---|
| `providers.tf` | AWS + Cloudflare + null providers, pinned versions |
| `backend.tf` | Remote state in S3 with a DynamoDB lock table. **State key: `videoguard-video-processing/terraform.tfstate`** (per-environment buckets `vg-tfstate-<env>`, lock table `vg-tfstate-lock`). |
| `variables.tf` | All inputs; the credential variables (`bunny_api_key`, `cloudflare_api_token`) are `sensitive = true`. The edge token secret and origin host are deliberately NOT Terraform variables — they are GitHub `secrets.EDGE_TOKEN_SECRET` / `vars.EDGE_ORIGIN_HOST`, used only by the CI `edge` job. |
| `data.tf` | Caller identity/region lookups, default-VPC subnets, shared locals |
| `s3.tf` | Media bucket, hardening, CORS, lifecycle, S3 event → SQS |
| `sqs.tf` | Jobs queue + DLQ, redrive (3 receives), S3 send policy |
| `iam.tf` | Worker instance profile, Railway backend user, Bunny origin user |
| `ecr.tf` | The two pinned image repositories (immutable tags, scan on push) |
| `asg.tf` | Launch template + ASG (min 0 / max 3) + scale-out-only policy |
| `security-groups.tf` | Egress-only security group (no inbound rules) |
| `cloudwatch.tf` | Worker log groups + five alarms |
| `bunny.tf` | Pull zone via `null_resource` + curl. The edge SCRIPT is NOT here — CI owns it (see below). |
| `cloudflare.tf` | DNS CNAME for the playback hostname |
| `outputs.tf` | Bucket/queue/ECR/ASG names, role ARN, sensitive access keys |
| `terraform.tfvars.example` | Placeholder values; copy to `terraform.tfvars` |
| `bootstrap-state.sh` | One-off, idempotent state-bucket + lock-table script |

## First-time setup (bootstrap the state)

```bash
cd infra
./bootstrap-state.sh dev     # creates vg-tfstate-dev + vg-tfstate-lock
# backend.tf: replace REPLACE_WITH_ENV with dev (or use -backend-config); the
# state key inside the bucket is videoguard-video-processing/terraform.tfstate
terraform init -backend-config="bucket=vg-tfstate-dev"
cp terraform.tfvars.example terraform.tfvars   # then fill in real values
terraform plan
```

The two edge parameters are NOT set in `terraform.tfvars` (they are not
Terraform variables at all). They live only in GitHub:
`secrets.EDGE_TOKEN_SECRET` (must equal Railway's
`PLAYBACK_EDGE_TOKEN_SECRET`) and `vars.EDGE_ORIGIN_HOST` (a hostname
distinct from the public CDN hostname, pointed at the same pull zone).

`bootstrap-state.sh` needs the `aws` CLI with credentials. It is idempotent:
every step checks for existence first, so re-running is safe.

**Applying is gated.** `terraform apply` runs in CI
(`.github/workflows/aws-pipeline.yml`, job `terraform-apply`) behind a manual
`workflow_dispatch` and the `production` environment. Do not apply from a
laptop for the production environment. The `terraform` job only runs
`fmt`/`init -backend=false`/`validate`; the real plan runs inside
`terraform-apply` with the real S3 backend and OIDC credentials.

## The Bunny decision: null_resource, not a provider

There are community Terraform providers for Bunny.net, but none that we can
trust to manage **both pull zones (AWS-signed origins, Origin Shield, cache
rules) and Edge Scripting deployments**. Inventing resources against an
unverified provider would hide real API mismatches behind a green plan. So:

- `bunny.tf` manages the pull zone through `null_resource` + `local-exec`
  curl calls to `https://api.bunny.net`, authorised with `var.bunny_api_key`
  (sensitive, never stored in state).
- The Bunny **origin credentials** (access key/secret of the
  `vg-bunny-origin-<env>` IAM user from `iam.tf`) are never handled by
  Terraform: paste them into the Bunny dashboard under the zone's AWS signing
  settings. `terraform output bunny_origin_access_key_id` prints the key id
  (the secret is a sensitive output).
- Leaving `var.bunny_api_key` empty makes the curl calls **skip with a
  printed notice** (exit 0), so plans/applies without Bunny credentials do
  not fail.
- **Deferred gate:** the exact Bunny API field names for the pull zone AND
  the EdgeRule `ActionType` enum must be verified against
  https://docs.bunny.net/reference/ before the first real apply. The
  payload follows the documented pull-zone shape as of Nov 2026. No real
  Bunny API call has been made.
- **The edge script is CI's property, not Terraform's (D2).** The CI
  `edge` job (`.github/workflows/aws-pipeline.yml`) is the SOLE owner of the
  Bunny edge script: it builds the bundle with
  `deno bundle --platform browser -o edge/bunny/deploy/entry.js
  edge/bunny/src/index.ts`, then idempotently creates/updates the script and
  publishes the bundle via the Bunny API. Terraform deliberately does NOT
  manage the script — no `compute/script` call, no `filesha256` of the
  bundle, nothing in state. The actual API deploy is a **deferred runtime
  gate**: it runs only when the repo variable `EDGE_DEPLOY_ENABLED` is
  `'true'` (plus the production environment approval); otherwise the job
  prints a `::warning::` annotation and stays green. See
  `../edge/bunny/README.md`.
- **The edge script's runtime parameters (`VG_EDGE_PARAMS`) are supplied by
  the same CI `edge` job**, because the script answers **500 to every
  request** without them (fail closed, see `../edge/bunny/README.md`). CI
  reads GitHub `secrets.EDGE_TOKEN_SECRET` and `vars.EDGE_ORIGIN_HOST` and
  wraps the pair as a JSON string
  (`{"tokenSecret":"...","originHost":"..."}`) under the `EdgeScriptParams`
  field of the create/update payload, failing the job with a `::error::`
  when either is empty. They are NOT Terraform variables — keeping them out
  of Terraform means they can never land in tfvars or Terraform state. Two
  requirements:
  - `EDGE_TOKEN_SECRET` MUST equal the `PLAYBACK_EDGE_TOKEN_SECRET` set on
    Railway (the Go `IssueSessionEdgeToken` issuer signs with the same
    secret), or every token fails verification (403);
  - `EDGE_ORIGIN_HOST` MUST be a hostname DISTINCT from the public CDN
    hostname that still points at the same pull zone — if it were the
    public hostname, the canonical fetch would re-enter the edge (see the
    recursion-guard notes in `../edge/bunny/README.md`).

## Timings and the scale-in rule

- `visibility_timeout_seconds = 900`, heartbeat every `300 s`
  (CONTRACT.md §2). A dead worker's message returns in ~15 min, not hours.
- The ASG scales **out only** on `ApproximateNumberOfMessagesVisible`
  (`disable_scale_in = true`): a received message is invisible, so scale-in
  on that metric could kill a busy worker. Each worker processes exactly one
  message and terminates itself; **self-termination is the ONLY scale-in
  path**.
- `estimated_instance_warmup = 300` matches the 2–4 min AMI boot budget.

## IAM scoping honesty

CONTRACT.md §8 writes the worker permissions as `incoming/<job>/*`. A static
instance-profile policy cannot carry a runtime value (the `job_id` is a UUID
created at upload time), so the static grant covers whole prefixes
(`incoming/*`, `jobs/*`, ...). Job isolation is enforced by the lifecycle:
one instance per message, `_LOCK` conditional writes, termination after each
job. Narrowing to per-job scopes (e.g. a bootstrap-time session policy) is a
deferred hardening step.

The Railway backend user has exactly: `s3:PutObject` on `jobs/*` and
`incoming/*` (sidecar writes + the presigner's credentials), the READ grants
the Go control plane needs (audit fix): `s3:GetObject` on `jobs/*` (the
webhook handler re-reads the sidecar, `SidecarVideoResolver`) and
`s3:GetObject` + `s3:ListBucket` scoped to `reports/*` and `renditions/*`
(reconciliation, `internal/awscontrol/reconcile.go`), and
`sqs:SendMessage` on the jobs queue. **Rotate its access key** (and the
Bunny origin key) on a schedule; both secrets are sensitive outputs and must
live only in Railway secrets / the Bunny dashboard.

## Deferred gates (do NOT claim these ran locally)

No terraform/packer/aws CLI and no real accounts exist in this environment
(CONTRACT.md §11). What WAS verified locally:

- every `.tf`/`.pkr.hcl` file parses with `python-hcl2`
  (`../tests/infra/test_hcl_parse.py`),
- the CI workflow parses with PyYAML (`../tests/infra/test_ci_yaml.py`),
- the D2 ownership split is asserted by the additive parse test: the
  `edge_token_secret` / `edge_origin_host` variables do NOT exist anywhere
  in Terraform, no `.tf` file touches `compute/script` or `EdgeScriptParams`,
  and the CI `edge` job is the sole owner feeding `EDGE_TOKEN_SECRET` /
  `EDGE_ORIGIN_HOST` into the deploy step,
- the Bunny edge script passes `deno test` / `deno check`
  (`../edge/bunny/`).

What is NOT verified here and must run in CI or a real account:

- `terraform validate` / `plan` / `apply` (CI, gated),
- `packer validate` / `build` (CI, gated),
- S3 event → SQS delivery shape, queue policy acceptance,
- lifecycle `days = 0` GLACIER_IR transitions + the 128 KiB / 90-day minimums
  against a real bucket,
- Bunny API payload field names (pull zone, and the edge-script
  `EdgeScriptParams` field that carries the `VG_EDGE_PARAMS` parameters —
  now sent by the CI `edge` job) and the real edge runtime behaviour
  (parameter injection, the numeric script Id, and publish included),
- Cloudflare record apply and custom-hostname certificate issuance,
- ASG scale-out and worker self-termination on a real `g6.xlarge`.

## Pinned-version manifest note

`reports/<job_id>/manifest.json` (written by the worker) pins the generator
image digest, Shaka version, FFmpeg commit, AMI id and ladder per job. The
AMI itself is labelled at build time (`deploy/packer/worker.pkr.hcl` sets
`vg:build-date`, `vg:ffmpeg-sha`, `vg:shaka-version` tags), and ECR tags are
immutable. Keep the pin chain in one direction: Packer variables → AMI tags →
launch-template `user_data` → manifest.json. Never bump a pin without
rebuilding the image/AMI that carries it.
