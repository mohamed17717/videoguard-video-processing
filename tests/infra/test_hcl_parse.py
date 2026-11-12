"""Parse-only tests for the Terraform + Packer files.

CONTRACT.md section 10: there is no terraform/packer binary in this
environment, so `terraform validate` and `packer validate` are DEFERRED GATES
(they run in CI). What IS verified locally:

- every .tf / .pkr.hcl file parses with python-hcl2 without exceptions;
- the expected top-level blocks exist in the right files;
- no hard-coded AWS access keys or long secret-looking hex defaults;
- no heredocs (the contract asks for jsonencode-style HCL so python-hcl2
  and future tooling parse everything deterministically).

python-hcl2 shape notes (v8), handled by the helpers below:
- repeated blocks of one keyword become a LIST of single-entry dicts;
- block labels and string values keep their literal quotes;
- blocks carry a __is_block__ marker and comments a __comments__ key.
"""

from __future__ import annotations

import re

import hcl2
import pytest

from conftest import (
    EDGE_BUNDLE_PATH,
    INFRA_DIR,
    PACKER_DIR,
    PACKER_FILENAMES,
    TERRAFORM_FILENAMES,
)

AWS_KEY_PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")
LONG_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{32,}\b")


# ---------------------------------------------------------------- helpers --

def _unquote(value: str) -> str:
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _clean(value):
    """Recursively unquote strings and drop the __markers__."""
    if isinstance(value, dict):
        return {
            _unquote(k): _clean(v)
            for k, v in value.items()
            if not k.startswith("__")
        }
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return _unquote(value)
    return value


def _two_label_blocks(data: dict, keyword: str) -> dict[str, dict[str, dict]]:
    """resource/data/variable/etc -> {type: {name: cleaned block}}."""
    out: dict[str, dict[str, dict]] = {}
    for entry in data.get(keyword, []):
        for block_type, names in _clean(entry).items():
            out.setdefault(block_type, {}).update(names)
    return out


def load_infra(name: str) -> dict:
    text = (INFRA_DIR / name).read_text(encoding="utf-8")
    return hcl2.loads(text)


def load_packer(name: str) -> dict:
    text = (PACKER_DIR / name).read_text(encoding="utf-8")
    return hcl2.loads(text)


def infra_text(name: str) -> str:
    return (INFRA_DIR / name).read_text(encoding="utf-8")


# ------------------------------------------------------------- parse all --


@pytest.mark.parametrize("name", TERRAFORM_FILENAMES)
def test_terraform_file_parses(name: str) -> None:
    data = load_infra(name)
    assert isinstance(data, dict), f"{name}: hcl2 did not return a dict"


@pytest.mark.parametrize("name", PACKER_FILENAMES)
def test_packer_file_parses(name: str) -> None:
    data = load_packer(name)
    assert isinstance(data, dict), f"{name}: hcl2 did not return a dict"


def test_tfvars_example_parses() -> None:
    data = hcl2.loads(infra_text("terraform.tfvars.example"))
    flattened = {key: _clean(value) for key, value in data.items() if not key.startswith("__")}
    assert "env" in flattened
    assert "bunny_api_key" in flattened
    assert "cloudflare_api_token" in flattened


def test_bootstrap_script_is_a_valid_skeleton() -> None:
    text = (INFRA_DIR / "bootstrap-state.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "vg-tfstate" in text
    assert "DEFERRED GATE" in text, "the script must state its deferred gate"


# ------------------------------------------------------- expected blocks --


def test_providers_declare_all_three_providers() -> None:
    data = load_infra("providers.tf")
    terraform = _clean(data["terraform"])[0]
    required = terraform["required_providers"][0]
    for provider in ("aws", "cloudflare", "null"):
        assert provider in required, f"missing required provider {provider}"
    # Cloudflare is pinned to v4 (the CNAME resource lives there).
    assert required["cloudflare"]["version"] == "~> 4.0"


def test_backend_is_s3_with_lock_table() -> None:
    data = load_infra("backend.tf")
    backend_blocks = _clean(data["terraform"])[0]["backend"]
    assert "s3" in backend_blocks[0]
    s3_backend = backend_blocks[0]["s3"]
    assert s3_backend["dynamodb_table"] == "vg-tfstate-lock"
    assert s3_backend["encrypt"] is True


def test_s3_tf_declares_bucket_and_hardening() -> None:
    resources = _two_label_blocks(load_infra("s3.tf"), "resource")
    for expected in (
        "aws_s3_bucket",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_policy",
        "aws_s3_bucket_cors_configuration",
        "aws_s3_bucket_lifecycle_configuration",
        "aws_s3_bucket_notification",
    ):
        assert expected in resources, f"s3.tf is missing resource {expected}"
    assert "media" in resources["aws_s3_bucket"]

    pab = resources["aws_s3_bucket_public_access_block"]["media"]
    assert pab["block_public_acls"] is True
    assert pab["block_public_policy"] is True
    assert pab["ignore_public_acls"] is True
    assert pab["restrict_public_buckets"] is True

    versioning = resources["aws_s3_bucket_versioning"]["media"]
    assert versioning["versioning_configuration"][0]["status"] == "Disabled"

    notification = resources["aws_s3_bucket_notification"]["media"]
    queue_event = notification["queue"][0]
    assert queue_event["events"] == ["s3:ObjectCreated:Put"], (
        "MUST be ObjectCreated:Put, never ObjectCreated:* (loop guard)"
    )
    assert queue_event["filter_prefix"] == "incoming/"
    assert queue_event["filter_suffix"] == ".mp4"


def test_s3_lifecycle_rules_cover_both_retentions() -> None:
    resources = _two_label_blocks(load_infra("s3.tf"), "resource")
    rules = resources["aws_s3_bucket_lifecycle_configuration"]["media"]["rule"]
    rule_ids = {rule["id"] for rule in rules}
    assert {"archive-incoming", "archive-masters", "archive-renditions", "ttl30-expire"} <= rule_ids

    archive = next(rule for rule in rules if rule["id"] == "archive-incoming")
    transition = archive["transition"][0]
    assert transition["storage_class"] == "GLACIER_IR"
    assert transition["days"] == 0

    ttl = next(rule for rule in rules if rule["id"] == "ttl30-expire")
    assert ttl["expiration"][0]["days"] == 30
    assert "transition" not in ttl, "ttl30 objects must never hit Glacier"


def test_sqs_tf_declares_queue_dlq_and_policy() -> None:
    resources = _two_label_blocks(load_infra("sqs.tf"), "resource")
    assert {"jobs", "dlq"} <= set(resources["aws_sqs_queue"])
    assert "aws_sqs_queue_policy" in resources

    jobs = resources["aws_sqs_queue"]["jobs"]
    assert jobs["message_retention_seconds"] == 345600
    redrive = jobs["redrive_policy"]
    match = re.search(r"maxReceiveCount\s*=\s*(\d+)", redrive)
    assert match and match.group(1) == "3", f"maxReceiveCount must be 3: {redrive}"

    dlq = resources["aws_sqs_queue"]["dlq"]
    assert dlq["message_retention_seconds"] == 1209600


def test_iam_tf_declares_three_identities() -> None:
    resources = _two_label_blocks(load_infra("iam.tf"), "resource")
    assert "worker" in resources["aws_iam_role"]
    assert "worker" in resources["aws_iam_instance_profile"]
    assert {"backend", "bunny_origin"} <= set(resources["aws_iam_user"])
    assert {"backend", "bunny_origin"} <= set(resources["aws_iam_access_key"])


def test_worker_policy_has_the_contract_actions() -> None:
    data = load_infra("iam.tf")
    docs = _two_label_blocks(data, "data")["aws_iam_policy_document"]
    statements = docs["worker"]["statement"]

    allowed: set[str] = set()
    denied: set[str] = set()
    for statement in statements:
        target = denied if statement["effect"] == "Deny" else allowed
        target.update(statement["actions"])

    for required in (
        "s3:GetObject",
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:ListBucket",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:SendMessage",
        "secretsmanager:GetSecretValue",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    ):
        assert required in allowed, f"worker policy is missing {required}"
    assert denied == {"s3:PutObject"}, "the ONLY deny action must be s3:PutObject"

    # Tagging must ALSO cover incoming/ (processed=true tag on the original).
    tagging_statements = [
        statement["resources"]
        for statement in statements
        if "s3:PutObjectTagging" in statement.get("actions", [])
    ]
    assert tagging_statements and any(
        "/incoming/*" in str(resource) for resource in tagging_statements[0]
    ), "s3:PutObjectTagging must include incoming/*"


def test_worker_policy_has_a_scoped_list_bucket_for_head_object() -> None:
    """Fix #4: HeadObject on a MISSING key returns 403 (not 404) without
    s3:ListBucket, and done_exists() treats only 404 as absence. The worker
    grant is bucket-level but scoped by s3:prefix to the prefixes it HEADs:
    reports/ (_DONE, _LOCK) and renditions/ (uploaded segments)."""
    data = load_infra("iam.tf")
    docs = _two_label_blocks(data, "data")["aws_iam_policy_document"]
    statements = docs["worker"]["statement"]

    list_statements = [
        statement for statement in statements if "s3:ListBucket" in statement["actions"]
    ]
    assert len(list_statements) == 1, "s3:ListBucket must be one scoped statement"
    statement = list_statements[0]
    assert statement["effect"] == "Allow"
    assert statement["resources"] == ["arn:aws:s3:::${local.bucket_name}"], (
        "s3:ListBucket is bucket-level; the scope must come from the condition"
    )
    condition = statement["condition"][0]
    assert condition["test"] == "StringLike"
    assert condition["variable"] == "s3:prefix"
    assert set(condition["values"]) == {"reports/*", "renditions/*"}

    # The explicit incoming/ deny stays exactly as it was.
    denied = [
        statement
        for statement in statements
        if statement["effect"] == "Deny" and "s3:PutObject" in statement["actions"]
    ]
    assert len(denied) == 1
    assert denied[0]["resources"] == ["arn:aws:s3:::${local.bucket_name}/incoming/*"]


def test_backend_policy_has_the_audit_read_grants() -> None:
    """DeepSeek audit fix: the Railway backend policy must cover the reads the
    Go control plane performs with its own credentials - the webhook handler
    re-reads the sidecar (SidecarVideoResolver), reconciliation lists + reads
    reports/ and renditions/ (reconcile.go listKeys), and abandoned-upload
    cleanup HeadObjects incoming/ (a missing key is 403 without ListBucket)."""
    data = load_infra("iam.tf")
    docs = _two_label_blocks(data, "data")["aws_iam_policy_document"]
    statements = docs["backend"]["statement"]

    # s3:GetObject on jobs/* (sidecar), reports/* and renditions/*.
    get_object_resources = {
        resource
        for statement in statements
        if "s3:GetObject" in statement["actions"]
        for resource in statement["resources"]
    }
    for prefix in ("/jobs/*", "/incoming/*", "/reports/*", "/renditions/*"):
        assert any(
            prefix in resource for resource in get_object_resources
        ), f"backend policy is missing s3:GetObject on {prefix}"

    # s3:ListBucket on the bucket, scoped by the s3:prefix condition.
    list_statements = [
        statement for statement in statements if "s3:ListBucket" in statement["actions"]
    ]
    assert len(list_statements) == 1, "s3:ListBucket must be one scoped statement"
    list_statement = list_statements[0]
    assert list_statement["resources"] == ["arn:aws:s3:::${local.bucket_name}"], (
        "s3:ListBucket is bucket-level; the scope must come from the condition"
    )
    condition = list_statement["condition"][0]
    assert condition["test"] == "StringLike"
    assert condition["variable"] == "s3:prefix"
    # incoming/* is required by CleanupAbandonedUploads: HeadObject on a
    # missing source object returns 403 (not 404) without ListBucket.
    assert set(condition["values"]) == {"incoming/*", "reports/*", "renditions/*"}

    # The write + dispatch grants must be unchanged.
    put_object_resources = " ".join(
        resource
        for statement in statements
        if "s3:PutObject" in statement["actions"]
        for resource in statement["resources"]
    )
    assert "/jobs/*" in put_object_resources and "/incoming/*" in put_object_resources, (
        "s3:PutObject must still cover jobs/* and incoming/*"
    )
    assert any(
        "sqs:SendMessage" in statement["actions"] for statement in statements
    ), "sqs:SendMessage on the jobs queue must stay"


def test_backend_policy_has_incoming_headobject_grant() -> None:
    """B8: CleanupAbandonedUploads calls HeadObject on
    incoming/<job_id>/source.mp4 to tell an abandoned upload (404) from a
    successful PUT whose upload-complete ack was lost. HeadObject needs
    s3:GetObject on incoming/* in the Railway/backend policy."""
    data = load_infra("iam.tf")
    docs = _two_label_blocks(data, "data")["aws_iam_policy_document"]
    statements = docs["backend"]["statement"]

    get_object_resources = {
        resource
        for statement in statements
        if "s3:GetObject" in statement["actions"]
        for resource in statement["resources"]
    }
    assert any("/incoming/*" in resource for resource in get_object_resources), (
        "backend policy is missing s3:GetObject on incoming/* "
        "(HeadObject for abandoned-upload cleanup)"
    )


def test_lifecycle_filters_match_what_the_worker_tags() -> None:
    """Audit fix: the worker tags OUTPUTS with vg-retention ONLY; processed=true
    is reserved for the ORIGINAL in incoming/. A filter requiring both tags on
    masters/renditions never matched anything."""
    resources = _two_label_blocks(load_infra("s3.tf"), "resource")
    rules = resources["aws_s3_bucket_lifecycle_configuration"]["media"]["rule"]

    incoming = next(rule for rule in rules if rule["id"] == "archive-incoming")
    assert incoming["filter"][0]["and"][0]["tags"] == {
        "vg-retention": "archive",
        "processed": "true",
    }, "archive-incoming must keep BOTH tags (the original is tagged twice)"

    for rule_id in ("archive-masters", "archive-renditions"):
        rule = next(rule for rule in rules if rule["id"] == rule_id)
        tags = rule["filter"][0]["and"][0]["tags"]
        assert tags == {"vg-retention": "archive"}, (
            f"{rule_id} must filter on vg-retention only, got {tags}"
        )
        assert "processed" not in tags, (
            f"{rule_id} must not require processed=true (outputs never get it)"
        )


def test_bunny_tf_manages_only_the_pull_zone() -> None:
    """D2: Terraform owns ONLY the pull zone; the edge script is CI's job
    (.github/workflows/aws-pipeline.yml, job `edge`). The pull-zone payload
    is still hashed into its triggers so an apply re-runs the API call when
    the desired config changes. Terraform must NOT hash the edge bundle or
    address the compute/script endpoint at all."""
    data = load_infra("bunny.tf")
    resources = _two_label_blocks(data, "resource")["null_resource"]
    assert set(resources) == {"bunny_pull_zone"}, (
        f"Terraform must manage ONLY the pull zone, got {sorted(resources)}"
    )
    triggers = resources["bunny_pull_zone"]["triggers"]
    assert "payload_hash" in triggers
    assert "sha256(" in str(triggers["payload_hash"])
    text = infra_text("bunny.tf")
    assert "compute/script" not in text, "Terraform must NOT create the edge script"
    assert "filesha256" not in text, "Terraform must NOT hash the edge bundle"
    assert "edge/bunny/deploy/entry.js" not in text, (
        "the edge bundle is built + published by CI, not Terraform"
    )
    # CI still owns the built bundle as a tracked artifact.
    assert EDGE_BUNDLE_PATH.is_file(), "deploy/entry.js must exist as an artifact"
    assert "__VG_VARIANT__" in EDGE_BUNDLE_PATH.read_text(encoding="utf-8")


def test_ecr_tf_declares_both_repositories_immutable_and_scanned() -> None:
    resources = _two_label_blocks(load_infra("ecr.tf"), "resource")
    assert set(resources["aws_ecr_repository"]) == {"generator", "validator"}
    for name in ("generator", "validator"):
        repo = resources["aws_ecr_repository"][name]
        assert repo["image_tag_mutability"] == "IMMUTABLE"
        assert repo["image_scanning_configuration"][0]["scan_on_push"] is True
    assert "aws_ecr_lifecycle_policy" in resources


def test_asg_tf_declares_template_group_and_scale_out_policy() -> None:
    resources = _two_label_blocks(load_infra("asg.tf"), "resource")
    for expected in (
        "aws_launch_template",
        "aws_autoscaling_group",
        "aws_autoscaling_policy",
        "aws_cloudwatch_metric_alarm",
    ):
        assert expected in resources, f"asg.tf is missing resource {expected}"

    template = resources["aws_launch_template"]["worker"]
    assert template["instance_initiated_shutdown_behavior"] == "terminate"
    metadata = template["metadata_options"][0]
    assert metadata["http_tokens"] == "required"
    assert metadata["http_endpoint"] == "enabled"
    ebs = template["block_device_mappings"][0]["ebs"][0]
    assert ebs["volume_size"] == 100
    assert ebs["volume_type"] == "gp3"
    assert ebs["encrypted"] is True
    assert ebs["delete_on_termination"] is True
    # user_data must be jsonencode-based (heredocs are forbidden repo-wide).
    user_data = template["user_data"]
    assert "base64encode(jsonencode(" in user_data
    # Fix #1: no launch-template -> ASG -> launch-template cycle.
    assert "local.worker_asg_name" in user_data
    assert "aws_autoscaling_group.worker.name" not in user_data
    # Fix #3: VG_CONFIG_PATH is the PIPELINE config, not worker.yaml (which
    # already points pipeline_config_path at /etc/vg/pipeline.yaml).
    assert "/etc/vg/pipeline.yaml" in user_data
    assert '"/etc/vg/worker.yaml"' not in user_data

    group = resources["aws_autoscaling_group"]["worker"]
    assert group["name"] == "${local.worker_asg_name}"

    # Fix #20a: zero -> one via a raw visible-count alarm + step scaling.
    step_policy = resources["aws_autoscaling_policy"]["scale_out_from_zero"]
    assert step_policy["policy_type"] == "StepScaling"
    assert step_policy["adjustment_type"] == "ChangeInCapacity"
    assert step_policy["metric_aggregation_type"] == "Maximum"
    # The provider rejects `cooldown` on StepScaling; the 300 s throttle is
    # estimated_instance_warmup (instances are not warm for 5 minutes).
    assert step_policy["estimated_instance_warmup"] == 300
    assert "cooldown" not in step_policy
    assert step_policy["step_adjustment"][0]["scaling_adjustment"] == 1
    assert step_policy["step_adjustment"][0]["metric_interval_lower_bound"] == 0
    assert "scaling_adjustment" not in step_policy, "no simple-scaling field"

    alarm = resources["aws_cloudwatch_metric_alarm"]["queue_waiting_scale_out"]
    assert alarm["namespace"] == "AWS/SQS"
    assert alarm["metric_name"] == "ApproximateNumberOfMessagesVisible"
    assert alarm["statistic"] == "Maximum"
    assert alarm["comparison_operator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["threshold"] == 1
    assert alarm["period"] == 60
    assert alarm["alarm_actions"] == [
        "${aws_autoscaling_policy.scale_out_from_zero.arn}"
    ]

    # Fix #20b: sustained backlog uses metric math on visible / inservice.
    policy = resources["aws_autoscaling_policy"]["scale_out_on_backlog"]
    assert policy["estimated_instance_warmup"] == 300
    tracking = policy["target_tracking_configuration"][0]
    assert tracking["disable_scale_in"] is True
    assert tracking["target_value"] == 1
    spec = tracking["customized_metric_specification"][0]
    queries = {query["id"]: query for query in spec["metrics"]}
    assert set(queries) == {"visible", "inservice", "backlog_per_instance"}
    assert queries["backlog_per_instance"]["expression"] == (
        "IF(inservice > 0, visible / inservice, visible)"
    )
    assert queries["backlog_per_instance"]["return_data"] is True
    visible_metric = queries["visible"]["metric_stat"][0]["metric"][0]
    in_service_metric = queries["inservice"]["metric_stat"][0]["metric"][0]
    assert visible_metric["namespace"] == "AWS/SQS"
    assert visible_metric["metric_name"] == "ApproximateNumberOfMessagesVisible"
    assert visible_metric["dimensions"][0]["name"] == "QueueName"
    assert in_service_metric["namespace"] == "AWS/AutoScaling"
    assert in_service_metric["metric_name"] == "GroupInServiceInstances"
    assert in_service_metric["dimensions"][0]["name"] == "AutoScalingGroupName"

    # Self-termination is the only scale-in path: no policy may remove
    # capacity.
    for name, candidate in resources["aws_autoscaling_policy"].items():
        if candidate["policy_type"] == "TargetTrackingScaling":
            assert candidate["target_tracking_configuration"][0]["disable_scale_in"] is True
        else:
            assert candidate["step_adjustment"][0]["scaling_adjustment"] > 0, (
                f"{name} must only scale out"
            )


def test_asg_name_has_no_launch_template_cycle_and_policies_depend_on_asg() -> None:
    data = load_infra("asg.tf")
    locals_merged: dict = {}
    for block in _clean(data["locals"]):
        locals_merged.update(block)
    assert locals_merged["worker_asg_name"] == "vg-worker-${var.env}"

    resources = _two_label_blocks(data, "resource")
    launch_user_data = str(resources["aws_launch_template"]["worker"]["user_data"])
    assert "local.worker_asg_name" in launch_user_data, (
        "the launch template must use the local name"
    )
    assert "aws_autoscaling_group.worker.name" not in launch_user_data, (
        "the launch template must NOT reference the ASG resource (cycle)"
    )

    # The policies are not referenced BY the ASG, so they MAY and SHOULD
    # reference the ASG resource to guarantee create ordering on a fresh apply.
    policies = resources["aws_autoscaling_policy"]
    for name in ("scale_out_from_zero", "scale_out_on_backlog"):
        assert "aws_autoscaling_group.worker.name" in str(
            policies[name]["autoscaling_group_name"]
        ), f"{name} must depend on the ASG resource"


def test_security_group_has_no_ingress_and_honest_egress() -> None:
    resources = _two_label_blocks(load_infra("security-groups.tf"), "resource")
    sg = resources["aws_security_group"]["worker"]
    assert "ingress" not in sg, "the worker SG must have NO ingress rules"
    egress = sg["egress"][0]
    assert egress["from_port"] == 443
    assert egress["cidr_blocks"] == ["0.0.0.0/0"]
    # Network honesty: never CLAIM "no 0.0.0.0/0 egress"; the boundary is the
    # ephemeral instance (CONTRACT.md section 8).
    text = infra_text("security-groups.tf")
    assert 'Do NOT claim "no 0.0.0.0/0 egress"' in text


def test_cloudwatch_declares_log_groups_and_five_alarms() -> None:
    resources = _two_label_blocks(load_infra("cloudwatch.tf"), "resource")
    assert set(resources["aws_cloudwatch_log_group"]) == {
        "worker_supervisor", "worker_container", "worker_ffmpeg",
    }
    alarms = resources["aws_cloudwatch_metric_alarm"]
    assert len(alarms) >= 5
    dlq_alarm = alarms["dlq_not_empty"]
    assert dlq_alarm["threshold"] == 0
    assert dlq_alarm["dimensions"]["QueueName"] == "${local.dlq_name}"


def test_bunny_tf_uses_null_resource_with_local_exec() -> None:
    resources = _two_label_blocks(load_infra("bunny.tf"), "resource")
    # D2: ONLY the pull zone remains. The edge script is CI's responsibility.
    assert set(resources["null_resource"]) == {"bunny_pull_zone"}
    text = infra_text("bunny.tf")
    assert 'provisioner "local-exec"' in text
    assert "api.bunny.net" in text
    assert "AccessKey:" in text
    # The API key is interpolated from the sensitive variable, never literal.
    assert "${var.bunny_api_key}" in text
    assert "compute/script" not in text, "no edge-script create call in Terraform"


def test_cloudflare_tf_declares_the_cname() -> None:
    resources = _two_label_blocks(load_infra("cloudflare.tf"), "resource")
    record = resources["cloudflare_record"]["pull_zone_cname"]
    assert record["type"] == "CNAME"
    assert record["proxied"] is False


def test_outputs_cover_the_operator_surface() -> None:
    outputs = _two_label_blocks(load_infra("outputs.tf"), "output")
    for expected in (
        "bucket_name",
        "bucket_arn",
        "jobs_queue_url",
        "dlq_queue_arn",
        "generator_ecr_url",
        "validator_ecr_url",
        "asg_name",
        "worker_role_arn",
        "pull_zone_hostname",
        "backend_access_key_secret",
        "bunny_origin_access_key_secret",
    ):
        assert expected in outputs, f"missing output {expected}"
    for sensitive in ("backend_access_key_secret", "bunny_origin_access_key_secret"):
        assert outputs[sensitive]["sensitive"] is True


# ----------------------------------------------------- secrets + style ----


@pytest.mark.parametrize("name", TERRAFORM_FILENAMES + PACKER_FILENAMES)
def test_no_hardcoded_aws_keys_or_heredocs(name: str) -> None:
    directory = PACKER_DIR if name.endswith(".pkr.hcl") else INFRA_DIR
    text = (directory / name).read_text(encoding="utf-8")
    assert AWS_KEY_PATTERN.search(text) is None, f"{name}: hard-coded AKIA key"
    assert "<<" not in text, f"{name}: heredocs are forbidden (use jsonencode)"


def test_secret_variables_are_marked_sensitive() -> None:
    variables = _two_label_blocks(load_infra("variables.tf"), "variable")
    for name in ("bunny_api_key", "cloudflare_api_token"):
        assert name in variables, f"missing variable {name}"
        assert variables[name]["sensitive"] is True, f"{name} must be sensitive = true"


def test_variable_defaults_hold_no_secret_shaped_values() -> None:
    variables = _two_label_blocks(load_infra("variables.tf"), "variable")
    for name, block in variables.items():
        default = block.get("default")
        if default is None:
            continue
        value = str(default)
        assert AWS_KEY_PATTERN.search(value) is None, f"{name} default looks like an AWS key"
        assert LONG_HEX_PATTERN.search(value) is None, (
            f"{name} default contains a long hex blob that looks like a secret"
        )


def test_tfvars_example_holds_placeholders_not_secrets() -> None:
    text = infra_text("terraform.tfvars.example")
    assert AWS_KEY_PATTERN.search(text) is None
    for line in text.splitlines():
        stripped = line.strip()
        for credential in ("bunny_api_key", "cloudflare_api_token"):
            if stripped.startswith(credential):
                value = stripped.split("=", 1)[1].strip().strip('"')
                assert value.startswith("REPLACE_WITH_"), (
                    f"{credential} placeholder must be an obvious REPLACE_WITH_ value"
                )


# ------------------------------------------------------------ packer ------


def test_packer_declares_amazon_ebs_source_and_build() -> None:
    data = load_packer("worker.pkr.hcl")
    sources = _clean(data["source"])
    assert "amazon-ebs" in sources[0]
    worker = sources[0]["amazon-ebs"]["worker"]
    assert worker["instance_type"] == "${var.instance_type}"
    assert worker["ssh_username"] == "${var.ssh_username}"
    # Fix #5: `tags` is the amazon-ebs option applied to the AMI; `ami_tags`
    # is not part of this builder and fails `packer validate`.
    assert "tags" in worker
    assert "ami_tags" not in worker
    assert worker["tags"]["vg:ffmpeg-sha"] == "${var.ffmpeg_commit}"
    assert "vg:build-date" in worker["run_tags"]

    # Fix #26b: Packer's temporary instance profile lets the build host
    # authenticate the best-effort pre-pull without an external role.
    iam = worker["temporary_iam_instance_profile_policy_document"][0]
    statement = iam["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert set(statement["Action"]) == {
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert statement["Resource"] == ["*"]

    assert "build" in data
    build = _clean(data["build"])[0]
    assert build["sources"] == ["source.amazon-ebs.worker"]
    # Each provisioner block parses as {"<kind>": {...}}.
    provisioners = build["provisioner"]
    provisioner_kinds = {kind for p in provisioners for kind in p}
    assert "shell" in provisioner_kinds, "the AMI must provision via shell steps"
    assert "file" in provisioner_kinds, "the checkout pieces must be copied in"

    # Fix #6: Packer's file provisioner creates only the FINAL directory, so
    # /tmp/vg-repo/deploy must exist before the first upload.
    assert "shell" in provisioners[0]
    assert "mkdir -p /tmp/vg-repo/deploy" in provisioners[0]["shell"]["inline"]
    first_file_index = next(i for i, p in enumerate(provisioners) if "file" in p)
    assert first_file_index == 1, "the mkdir provisioner must come first"

    # Fix #7: every shell step runs as root through sudo + bash. Packer's
    # defaults (no sudo, /bin/sh -e/dash) break set -o pipefail and apt-get.
    shell_provisioners = [p["shell"] for p in provisioners if "shell" in p]
    assert len(shell_provisioners) == 10
    expected_command = (
        "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    )
    for shell in shell_provisioners:
        assert shell["execute_command"] == expected_command
        assert shell["inline_shebang"] == "#!/bin/bash -e"


def test_packer_variables_cover_the_pins() -> None:
    data = load_packer("variables.pkr.hcl")
    variables = {k: v for entry in _clean(data["variable"]) for k, v in entry.items()}
    for name in (
        "region",
        "instance_type",
        "ami_name_prefix",
        "source_ami_name",
        "shaka_version",
        "ffmpeg_commit",
        "generator_image",
        "validator_image",
    ):
        assert name in variables, f"packer variables are missing {name}"
    assert variables["shaka_version"]["default"] == "3.2.0"
    assert variables["ssh_username"]["default"] == "ubuntu"
    # The pinned FFmpeg commit must equal the generator image's pin.
    assert variables["ffmpeg_commit"]["default"].startswith("3a0867c2bfda")


# ------------------------------- D2 edge ownership (CI, not Terraform) -----


def test_edge_secret_is_ci_only_never_in_terraform_state() -> None:
    """D2 ownership split + B6 fail-closed wiring.

    Terraform has NO edge-token/origin variables, NO edge-script create call
    and NO secret in state. The CI `edge` job is the SOLE owner: it creates
    (idempotently), publishes and injects VG_EDGE_PARAMS (tokenSecret +
    originHost), and fails closed when either is empty. The token secret is
    therefore a GitHub secret only - it can never land in tfvars/state
    (CONTRACT.md sections 10 and 11).
    """
    import yaml

    # variables.tf: the two edge parameters are GONE. They were GitHub
    # secrets.EDGE_TOKEN_SECRET / vars.EDGE_ORIGIN_HOST only - keeping them
    # as Terraform variables would put the secret in tfvars/state.
    variables = _two_label_blocks(load_infra("variables.tf"), "variable")
    assert "edge_token_secret" not in variables, (
        "edge_token_secret must NOT be a Terraform variable (CI owns it)"
    )
    assert "edge_origin_host" not in variables, (
        "edge_origin_host must NOT be a Terraform variable (CI owns it)"
    )

    # No .tf file may create the edge script, reference the removed secret
    # variables, or carry the VG_EDGE_PARAMS payload. Trigger values (and
    # resource attributes in general) land in Terraform state, so the secret
    # must not appear anywhere in the .tf files.
    for name in TERRAFORM_FILENAMES:
        text = infra_text(name)
        assert "compute/script" not in text, (
            f"{name}: Terraform must NOT manage the edge script (CI owns it)"
        )
        assert "edge_token_secret" not in text, (
            f"{name}: the token secret must be gone from Terraform"
        )
        assert "edge_origin_host" not in text, (
            f"{name}: the origin host must be gone from Terraform"
        )
        assert "EdgeScriptParams" not in text, (
            f"{name}: VG_EDGE_PARAMS belongs to the CI edge job only"
        )
        assert isinstance(load_infra(name), dict), f"{name}: hcl2 parse failed"

    # Belt-and-braces: ANY .tf file (even one not listed above) must not
    # manage the edge script.
    for tf_file in sorted(INFRA_DIR.glob("*.tf")):
        assert "compute/script" not in tf_file.read_text(encoding="utf-8"), (
            f"{tf_file.name}: Terraform must NOT manage the edge script"
        )

    # D2: the ONLY bunny null_resource is the pull zone.
    resources = _two_label_blocks(load_infra("bunny.tf"), "resource")["null_resource"]
    assert set(resources) == {"bunny_pull_zone"}, (
        f"Terraform must own ONLY the pull zone, got {sorted(resources)}"
    )

    # CI: the edge job owns create + publish + params and feeds both
    # parameters, failing closed when either is empty. INFRA_DIR is imported
    # at module load; its parent is the repo root, so derive the workflow
    # path from it (no flat-import shadowing across conftest modules).
    workflow_path = INFRA_DIR.parent / ".github" / "workflows" / "aws-pipeline.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    edge_job = workflow["jobs"]["edge"]
    dumped = yaml.safe_dump(edge_job)
    assert "secrets.EDGE_TOKEN_SECRET" in dumped, (
        "the edge job must read the EDGE_TOKEN_SECRET secret"
    )
    assert "vars.EDGE_ORIGIN_HOST" in dumped, (
        "the edge job must read the EDGE_ORIGIN_HOST repo variable"
    )
    run_text = "\n".join(str(step.get("run", "")) for step in edge_job["steps"])
    for needle in (
        "EDGE_TOKEN_SECRET",
        "EDGE_ORIGIN_HOST",
        "::error::",
        "EdgeScriptParams",
        "jq -cn --arg",
        "compute/script",
        "publish",
        "jq -r '.Id'",
    ):
        assert needle in run_text, f"the edge deploy step must contain {needle}"
