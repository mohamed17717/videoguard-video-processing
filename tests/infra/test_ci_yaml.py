"""CI workflow tests for .github/workflows/aws-pipeline.yml.

Verified with PyYAML (no GitHub runner locally). The workflow itself is the
CI gate for terraform/packer/edge deploy (CONTRACT.md section 11) - this test
only checks its structure: triggers, jobs, OIDC permissions, the image/packer
job-output wiring, the terraform-apply TF_VAR_* guards and the Bunny edge
script ownership. It also runs structural checks over ALL workflows under
.github/workflows (job-only keys must not sit on steps).

Repo-split facts reflected here:
  - paths are repo-root-relative (no video-processing/ prefix);
  - forensic-wm/ is a nested PUBLIC HTTPS submodule; only `images` checks it
    out recursively, and no ssh-agent is needed;
  - the Go backend lives in the video-guard repo (no Go toolchain here);
  - CI (job `edge`) is the SOLE owner of the Bunny edge script; Terraform
    deliberately does NOT manage it (infra/bunny.tf owns only the pull zone).
"""

from __future__ import annotations

import re

import yaml

from conftest import (
    CI_WORKFLOW_PATH,
    EDGE_BUNDLE_PATH,
    VIDEO_PROCESSING_ROOT,
    WORKFLOW_PATHS,
)

AWS_KEY_PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")


def _workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _raw_text() -> str:
    return CI_WORKFLOW_PATH.read_text(encoding="utf-8")


def _iter_steps(workflow: dict):
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


def _steps(workflow: dict, job_name: str) -> list[dict]:
    return workflow["jobs"][job_name].get("steps", [])


def _run_text(workflow: dict, job_name: str) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(workflow, job_name))


# ------------------------------------------------------------- basics ------


def test_workflow_parses_and_is_named() -> None:
    workflow = _workflow()
    assert workflow["name"] == "AWS video pipeline"


def test_triggers_cover_main_branch_and_dispatch() -> None:
    workflow = _workflow()
    triggers = workflow.get("on")
    assert triggers is not None, "the workflow must declare triggers"
    assert "push" in triggers and "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    # The standalone repo deploys from main (not the old hy4/aws-deploy branch).
    assert "main" in triggers["push"]["branches"]
    assert "main" in triggers["pull_request"]["branches"]
    assert "hy4/aws-deploy" not in str(triggers)


def test_required_jobs_exist() -> None:
    workflow = _workflow()
    jobs = set(workflow["jobs"].keys())
    assert {"validate", "images", "packer", "edge", "terraform", "terraform-apply"} <= jobs


def test_oidc_permission_is_declared() -> None:
    workflow = _workflow()
    permissions = workflow["permissions"]
    assert permissions["id-token"] == "write", (
        "id-token: write is required for AWS OIDC role assumption"
    )


# ------------------------------- nested submodule, no Go toolchain ----------


def test_no_ssh_agent_and_no_go_steps() -> None:
    """forensic-wm/ is a nested PUBLIC HTTPS submodule: no ssh-agent is needed
    anywhere. The Go backend is covered by video-guard/.github/workflows/ci.yml,
    so this workflow has no Go toolchain step either."""
    workflow = _workflow()
    for _job, step in _iter_steps(workflow):
        uses = str(step.get("uses", ""))
        assert "ssh-agent" not in uses, (
            "no ssh-agent step is needed (public HTTPS submodule)"
        )
    raw = _raw_text()
    assert "setup-go" not in raw
    assert "go-version-file" not in raw
    assert "go build" not in raw
    assert "go test" not in raw


def test_images_checks_out_nested_submodule_recursively() -> None:
    """Only `images` builds from forensic-wm/, so only its checkout must
    populate the nested submodule. Other jobs never need those files."""
    workflow = _workflow()
    checkouts = [
        step
        for step in _steps(workflow, "images")
        if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert len(checkouts) == 1, "images must have exactly one checkout step"
    assert (checkouts[0].get("with") or {}).get("submodules") == "recursive", (
        "images must use `submodules: recursive` so forensic-wm/ is populated"
    )
    for job_name, step in _iter_steps(workflow):
        uses = str(step.get("uses", ""))
        if job_name != "images" and "actions/checkout" in uses:
            assert (step.get("with") or {}).get("submodules") != "recursive", (
                f"only images needs the nested submodule, but {job_name} checks it out"
            )


def test_forensic_wm_is_a_nested_https_submodule() -> None:
    """.gitmodules pins forensic-wm/ to its own public HTTPS repo."""
    gitmodules = (VIDEO_PROCESSING_ROOT / ".gitmodules").read_text(encoding="utf-8")
    assert '[submodule "forensic-wm"]' in gitmodules
    assert "path = forensic-wm" in gitmodules
    assert (
        "url = https://github.com/mohamed17717/videoguard-forensic-watermark.git"
        in gitmodules
    )
    assert "git@" not in gitmodules, "the nested submodule must not need an SSH key"


def test_paths_are_repo_root_relative() -> None:
    """No stale video-processing/ prefix survives in any run/working-directory."""
    raw = _raw_text()
    assert "video-processing/" not in raw
    assert "pytest tests -q" in raw
    assert "pip install -e \".[dev]\"" in raw
    assert "pip install -r requirements-worker.txt" in raw
    assert "deno test --allow-read edge/bunny/tests/" in raw
    assert "deno check edge/bunny/src/*.ts" in raw
    assert "deno bundle" in raw
    assert "-o edge/bunny/deploy/entry.js" in raw
    assert "edge/bunny/src/index.ts" in raw
    assert "-f deploy/validator/Dockerfile" in raw
    assert "working-directory: infra" in raw
    assert "working-directory: deploy/packer" in raw


# ------------------------------------------------------------- images ------


def test_generator_build_context_is_forensic_wm() -> None:
    """CONTRACT.md section 0: the generator build context is forensic-wm/."""
    workflow = _workflow()
    generator_builds = [
        step
        for _job, step in _iter_steps(workflow)
        if "forensic-wm/deploy/generator/Dockerfile" in str(step.get("run", ""))
    ]
    assert generator_builds, "the generator image must be built from its Dockerfile"
    run_text = str(generator_builds[0]["run"])
    assert "-f forensic-wm/deploy/generator/Dockerfile" in run_text
    # The LAST token of the docker build command is the context directory.
    command = [line for line in run_text.splitlines() if "docker build" not in line]
    assert run_text.strip().endswith("forensic-wm"), (
        f"generator build context must be forensic-wm, got: ...{run_text.strip()[-60:]}"
    )
    assert command, "multi-line build command expected"


def test_validator_build_exists() -> None:
    workflow = _workflow()
    validator_builds = [
        step
        for _job, step in _iter_steps(workflow)
        if "deploy/validator/Dockerfile" in str(step.get("run", ""))
    ]
    assert validator_builds, "the validator image must be built in CI"
    run_text = str(validator_builds[0]["run"])
    assert "-f deploy/validator/Dockerfile" in run_text
    # The build context is the repo root (`.`), not video-processing/.
    assert run_text.strip().endswith(".")


def test_images_job_publishes_full_ecr_refs() -> None:
    """(d) images has outputs + a `refs` step AFTER the push that writes the
    two FULL ECR refs (and the registry) to $GITHUB_OUTPUT."""
    workflow = _workflow()
    images = workflow["jobs"]["images"]
    assert set(images["outputs"]) == {"generator_image", "validator_image", "registry"}
    for key in ("generator_image", "validator_image", "registry"):
        assert "steps.refs.outputs." in str(images["outputs"][key])

    steps = _steps(workflow, "images")
    ids = [step.get("id") for step in steps]
    assert "refs" in ids, "images must have an id=refs step"
    push_index = next(
        i for i, step in enumerate(steps) if "docker push" in str(step.get("run", ""))
    )
    refs_index = ids.index("refs")
    assert refs_index > push_index, "the refs step must run AFTER the push step"
    refs_run = str(steps[refs_index]["run"])
    assert "$GITHUB_OUTPUT" in refs_run
    assert "forensic-wm-generator:hevc-v1-${{ github.sha }}" in refs_run
    assert "vg-validator:pinned-${{ github.sha }}" in refs_run
    assert "registry=" in refs_run
    # The images job keeps its push/dispatch gate.
    assert "github.event_name == 'push'" in str(images["if"])
    assert "workflow_dispatch" in str(images["if"])


# ------------------------------------------------------------- packer ------


def test_packer_job_wiring_and_outputs() -> None:
    """(e) needs, job-level if/environment, ami_id output, gated build with
    the image -vars, ungated validate, OIDC and the manifest jq parse."""
    workflow = _workflow()
    packer = workflow["jobs"]["packer"]
    assert packer["needs"] == ["validate", "images"]
    assert "always()" in str(packer["if"])
    assert "needs.validate.result == 'success'" in str(packer["if"])
    assert all("environment" not in s for s in _steps(workflow, "packer")), (
        "environment belongs at job level, never on a packer step"
    )
    assert "ami_id" in packer["outputs"]

    steps = _steps(workflow, "packer")
    # packer validate is UNGATED (credential-free).
    validate_step = next(s for s in steps if "packer validate" in str(s.get("run", "")))
    assert "if" not in validate_step, "packer validate must stay ungated"
    # OIDC step present (build only).
    assert any(
        "aws-actions/configure-aws-credentials" in str(s.get("uses", "")) for s in steps
    ), "the packer build needs an OIDC credentials step"

    build = next(s for s in steps if s.get("id") == "build")
    assert "github.event_name == 'workflow_dispatch'" in str(build["if"])
    assert "needs.images.result == 'success'" in str(build["if"])
    build_run = str(build["run"])
    assert '-var "generator_image=${{ needs.images.outputs.generator_image }}"' in build_run
    assert '-var "validator_image=${{ needs.images.outputs.validator_image }}"' in build_run

    ami = next(s for s in steps if s.get("id") == "ami")
    ami_run = str(ami["run"])
    assert "packer-manifest.json" in ami_run
    assert "artifact_id" in ami_run
    assert 'split(":")[-1]' in ami_run, "strip the region:ami- prefix with jq"
    assert "ami_id" in ami_run
    assert "$GITHUB_OUTPUT" in ami_run


def test_packer_job_environment_is_job_level() -> None:
    """The exact audit bug: `environment: production` must live on the
    packer JOB, and no step in the workflow may carry `environment`."""
    workflow = _workflow()
    assert workflow["jobs"]["packer"].get("environment") == "production", (
        "jobs.packer.environment must be 'production' (job level)"
    )
    for job_name, step in _iter_steps(workflow):
        assert "environment" not in step, (
            f"aws-pipeline.yml: job '{job_name}' has a step with an "
            "'environment' key - environment belongs at job level"
        )


# -------------------------------------------------------------- edge -------


def test_edge_job_really_builds_the_bundle() -> None:
    """Audit fix: the edge job must BUILD deploy/entry.js with `deno bundle`
    before any deploy - a missing-bundle silent skip (exit 0) is forbidden."""
    workflow = _workflow()
    build_runs = [
        str(step.get("run", ""))
        for job, step in _iter_steps(workflow)
        if job == "edge" and "deno bundle" in str(step.get("run", ""))
    ]
    assert build_runs, "the edge job must run deno bundle"
    run = build_runs[0]
    assert "-o edge/bunny/deploy/entry.js" in run
    assert "edge/bunny/src/index.ts" in run
    assert "--platform browser" in run
    # The bundle is a tracked artifact CI owns; it must exist and still carry
    # the variant token the build check greps for.
    assert EDGE_BUNDLE_PATH.is_file(), "deploy/entry.js must exist as an artifact"
    assert "__VG_VARIANT__" in EDGE_BUNDLE_PATH.read_text(encoding="utf-8")


def test_edge_deploy_is_a_visible_gate_not_a_silent_skip() -> None:
    """The Bunny API deploy stays a DEFERRED RUNTIME GATE: gated on
    vars.EDGE_DEPLOY_ENABLED == 'true' (plus the production environment),
    and a disabled gate must print a visible ::warning:: annotation."""
    workflow = _workflow()
    edge_steps = _steps(workflow, "edge")
    gated = [
        step
        for step in edge_steps
        if "vars.EDGE_DEPLOY_ENABLED == 'true'" in str(step.get("if", ""))
    ]
    assert gated, "the Bunny API deploy must be gated on vars.EDGE_DEPLOY_ENABLED"
    warnings = [
        str(step.get("run", ""))
        for step in edge_steps
        if "::warning::" in str(step.get("run", ""))
    ]
    assert warnings, "a disabled gate must print a visible ::warning:: annotation"
    raw = _raw_text()
    assert "SKIPPED (deferred gate)" not in raw, "silent green skips are forbidden"
    assert "exit 0" not in raw.split("edge:")[1], (
        "the edge job must not silently exit 0 around a missing bundle"
    )


def test_edge_create_is_idempotent_and_publishes_by_numeric_id() -> None:
    """(f) CI is the SOLE owner. The create is idempotent: list-by-name first,
    then create (capture the numeric .Id with jq) OR update; publish goes to
    .../<id>/publish, NEVER to the script name. VG_EDGE_PARAMS carries the
    two parameters and the deploy fails closed on either being empty. The
    exact Bunny field/id handling remains a CONTRACT section 11 deferred gate."""
    workflow = _workflow()
    edge_steps = _steps(workflow, "edge")
    deploy = next(
        s for s in edge_steps if "Deploy the edge script" in str(s.get("name", ""))
    )
    run = str(deploy["run"])
    assert "compute/script" in run
    # Idempotent: list by name -> select numeric .Id; update existing with PUT.
    assert "select(.Name == $name)" in run, "list-by-name then reuse the numeric Id"
    assert "-X PUT" in run, "an existing script must be updated, not recreated"
    # Capture the create response's numeric .Id with jq.
    assert "jq -r '.Id'" in run, "the create response's numeric .Id must be captured"
    # Publish to the numeric id, NOT the script name.
    assert "$script_id/publish" in run, "publish to .../Id/publish"
    assert "$VG_EDGE_SCRIPT_NAME/publish" not in run, "never publish by script name"
    # VG_EDGE_PARAMS / EdgeScriptParams with both parameters.
    assert "VG_EDGE_PARAMS" in run
    assert "EdgeScriptParams" in run
    assert "tokenSecret" in run and "originHost" in run
    # B6 fail-closed guards.
    assert "::error::" in run
    assert "EDGE_TOKEN_SECRET" in run and "EDGE_ORIGIN_HOST" in run
    assert "jq -cn --arg" in run
    # Deferred gate is documented, not claimed as verified.
    assert "DEFERRED GATE" in run or "DEFERRED GATE" in _raw_text()
    assert "docs.bunny.net" in _raw_text()


def test_edge_script_is_owned_by_ci_not_terraform() -> None:
    """(STEP 3 / D2) CI owns create + publish + params; Terraform deliberately
    does NOT manage the edge script. The script name stays deterministic:
    vars.EDGE_SCRIPT_NAME with a vg-edge-prod default (env=prod ->
    vg-edge-${env}). The stale shared name 'vg-edge-script' is gone."""
    raw = _raw_text()
    assert "vg-edge-script" not in raw, "the stale shared script name must be gone"
    assert "vars.EDGE_SCRIPT_NAME" in raw
    assert "'vg-edge-prod'" in raw, "the prod default must match var.env=prod"

    workflow = _workflow()
    edge_steps = _steps(workflow, "edge")
    deploy = next(
        s for s in edge_steps if "Deploy the edge script" in str(s.get("name", ""))
    )
    deploy_run = str(deploy["run"])
    assert "compute/script" in deploy_run, "CI must CREATE the edge script"
    assert "publish" in deploy_run, "CI must PUBLISH the edge script"
    assert "EdgeScriptParams" in deploy_run, "CI must own VG_EDGE_PARAMS"

    # Terraform: only the pull zone; NO compute/script and NO edge-script shell.
    bunny_text = (VIDEO_PROCESSING_ROOT / "infra" / "bunny.tf").read_text(encoding="utf-8")
    assert "compute/script" not in bunny_text, "Terraform must NOT manage the edge script"
    assert "bunny_edge_script" not in bunny_text, "the edge-script null_resource is gone"
    assert 'null_resource" "bunny_edge_script"' not in bunny_text


def test_validate_job_runs_the_deno_gates() -> None:
    """The credential-free validate job must run the edge tests and the
    type-check (regression guard: they are what CI can really verify)."""
    workflow = _workflow()
    joined = _run_text(workflow, "validate")
    assert "deno test --allow-read edge/bunny/tests/" in joined
    assert "deno check edge/bunny/src/*.ts" in joined
    assert "pytest tests -q" in joined


# ---------------------------------------------------------- terraform ------


def test_terraform_job_has_no_pr_plan_or_placeholder_vars() -> None:
    """(g) fmt/init/validate stay; the PR plan and ALL placeholder -vars are
    gone. A -backend=false plan has no AWS credentials while infra/data.tf
    reads caller identity / default VPC, so it is not a real plan."""
    workflow = _workflow()
    terraform = workflow["jobs"]["terraform"]
    joined = _run_text(workflow, "terraform")
    assert "terraform fmt -check -recursive ." in joined
    assert "terraform init -backend=false" in joined
    assert "terraform validate" in joined
    assert "terraform plan" not in joined, "no PR plan in the credential-free job"
    assert terraform["defaults"]["run"]["working-directory"] == "infra"
    raw = _raw_text()
    assert "plan-placeholder" not in raw, "no placeholder -var values"
    assert "bunny_api_key=plan-placeholder" not in raw
    assert "-out=tfplan" in raw, "a real plan still happens in terraform-apply"


def test_terraform_apply_wiring() -> None:
    """(h) needs/if/environment/OIDC, the full TF_VAR_* set, test -n guards,
    the three -var build artifacts, and NO gitignored terraform.tfvars."""
    workflow = _workflow()
    apply = workflow["jobs"]["terraform-apply"]
    assert apply["needs"] == ["terraform", "images", "packer"]
    assert "workflow_dispatch" in str(apply["if"])
    assert apply["environment"] == "production"
    assert any(
        "aws-actions/configure-aws-credentials" in str(s.get("uses", ""))
        for s in _steps(workflow, "terraform-apply")
    ), "terraform-apply must assume the OIDC role"

    env = apply["env"]
    required = (
        "TF_VAR_env",
        "TF_VAR_webhook_host",
        "TF_VAR_webhook_path",
        "TF_VAR_pull_zone_hostname",
        "TF_VAR_cloudflare_zone_id",
        "TF_VAR_bunny_api_key",
        "TF_VAR_cloudflare_api_token",
        "TF_VAR_webhook_secret_arn",
        "TF_VAR_axinom_secret_arn",
        "TF_VAR_watermark_secret_arn",
    )
    for key in required:
        assert key in env, f"terraform-apply is missing {key}"
    assert str(env["TF_VAR_env"]) == "prod"

    apply_run = _run_text(workflow, "terraform-apply")
    assert "-var-file=terraform.tfvars" not in apply_run, (
        "terraform.tfvars is gitignored and must not be referenced"
    )
    # Fail-closed guards on Bunny/Cloudflare values AND the images/AMI outputs.
    guards = [s for s in _steps(workflow, "terraform-apply") if "test -n" in str(s.get("run", ""))]
    assert guards, "terraform-apply must FAIL on empty deploy inputs"
    guard_text = str(guards[0]["run"])
    for needle in (
        "TF_VAR_bunny_api_key",
        "TF_VAR_cloudflare_api_token",
        "TF_VAR_cloudflare_zone_id",
        "generator_image",
        "validator_image",
        "ami_id",
    ):
        assert needle in guard_text, f"test -n guard must cover {needle}"

    plan = next(
        s for s in _steps(workflow, "terraform-apply")
        if "terraform plan" in str(s.get("run", ""))
    )
    plan_run = str(plan["run"])
    assert '-var "ami_id=${{ needs.packer.outputs.ami_id }}"' in plan_run
    assert '-var "generator_image=${{ needs.images.outputs.generator_image }}"' in plan_run
    assert '-var "validator_image=${{ needs.images.outputs.validator_image }}"' in plan_run
    assert "terraform apply" in apply_run, "a real apply follows the real plan"


# ---------------------------------------------------------- credentials ----


def test_aws_credentials_use_oidc_not_static_keys() -> None:
    workflow = _workflow()
    oidc_steps = [
        step
        for _job, step in _iter_steps(workflow)
        if "aws-actions/configure-aws-credentials" in str(step.get("uses", ""))
    ]
    assert oidc_steps, "AWS auth must go through configure-aws-credentials"
    for step in oidc_steps:
        with_block = step.get("with") or {}
        assert "role-to-assume" in with_block
        assert "${{ secrets." in str(with_block["role-to-assume"]), (
            "role-to-assume must come from a secret"
        )
    raw = _raw_text()
    assert AWS_KEY_PATTERN.search(raw) is None, "no hard-coded AWS keys in CI"
    assert "aws_access_key_id" not in raw, "no static AWS keys in CI"


def test_no_hardcoded_secrets_in_the_workflow() -> None:
    raw = _raw_text()
    for pattern in ("AKIA", "ghp_", "-----BEGIN"):
        assert pattern not in raw, f"CI must not embed {pattern} credentials"
    # Credentials are referenced, not embedded.
    assert "${{ secrets.BUNNY_API_KEY }}" in raw


# --------------------------------- structural checks on ALL workflows ------


def _all_workflows() -> list[tuple[str, dict]]:
    """Every workflow under .github/workflows, parsed with PyYAML."""
    return [
        (path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in WORKFLOW_PATHS
    ]


# Keys that are valid at JOB level but ILLEGAL on a step. GitHub Actions
# rejects the whole workflow file when a step carries one of these, while
# PyYAML still parses the file fine - so only a structural check catches
# it. (`timeout-minutes` is valid at BOTH levels, so it is not here.)
STEP_FORBIDDEN_JOB_KEYS = [
    "environment",
    "runs-on",
    "needs",
    "permissions",
    "strategy",
    "outputs",
    "services",
    "container",
    "defaults",
    "concurrency",
]

# A code line that runs a compose command against the removed standalone
# `worker` service. Comments are cut off before matching: comments about
# the Python worker agent and the `requirements-worker.txt` install line
# are legitimate and must not be flagged.
WORKER_COMPOSE_COMMAND = re.compile(r"\b(up|stop|start|restart)\b[^#\n]*\bworker\b")


def _worker_compose_hits(raw: str) -> list[str]:
    """Code lines (comments cut off) that run compose on `worker`."""
    hits: list[str] = []
    for line in raw.splitlines():
        code = line.split("#", 1)[0]
        if WORKER_COMPOSE_COMMAND.search(code):
            hits.append(line.strip())
    return hits


def test_no_job_only_keys_on_steps() -> None:
    """Regression test for the audit bug: `environment: production` sat on a
    STEP of aws-pipeline.yml, which makes GitHub Actions reject the whole
    workflow. PyYAML parses such a file fine, so every step of every job in
    every workflow is checked against the job-only keys."""
    for workflow_name, workflow in _all_workflows():
        for job_name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", [])):
                label = step.get("name") or step.get("uses") or f"step {index}"
                for key in STEP_FORBIDDEN_JOB_KEYS:
                    assert key not in step, (
                        f"{workflow_name}: job '{job_name}', step '{label}' "
                        f"has the job-only key '{key}' - GitHub Actions "
                        "rejects the whole workflow for this"
                    )


def test_workflows_do_not_reference_removed_worker() -> None:
    """The worker compose service and Dockerfile.worker are removed. No
    workflow may build the image or run compose up/stop/start/restart for a
    standalone `worker` service."""
    for path in WORKFLOW_PATHS:
        raw = path.read_text(encoding="utf-8")
        assert "Dockerfile.worker" not in raw, (
            f"{path.name}: Dockerfile.worker was removed"
        )
        hits = _worker_compose_hits(raw)
        assert not hits, (
            f"{path.name}: compose commands naming the removed 'worker' "
            f"service: {hits}"
        )


def test_workflow_jobs_have_required_keys() -> None:
    """Every job needs `runs-on` and `steps` (or `uses` for a reusable
    workflow call), and every step needs `run` or `uses`. This catches
    workflows that GitHub Actions would refuse to run."""
    for workflow_name, workflow in _all_workflows():
        for job_name, job in workflow["jobs"].items():
            reusable = "uses" in job
            assert "runs-on" in job or reusable, (
                f"{workflow_name}: job '{job_name}' has no 'runs-on'"
            )
            assert "steps" in job or reusable, (
                f"{workflow_name}: job '{job_name}' has no 'steps'"
            )
            for index, step in enumerate(job.get("steps", [])):
                assert "run" in step or "uses" in step, (
                    f"{workflow_name}: job '{job_name}', step {index} "
                    f"('{step.get('name', '?')}') has neither 'run' nor 'uses'"
                )
