# Remote state: S3 bucket + DynamoDB lock table (CONTRACT.md section 10).
#
# The bucket and the lock table are created ONCE by infra/bootstrap-state.sh.
# That script is a one-off, idempotent shell script using the `aws` CLI.
# Running it is a DEFERRED GATE: there is no aws CLI in this environment.
#
# The bucket name below must match what bootstrap-state.sh created. Replace
# REPLACE_WITH_ENV with dev or prod, or override at init time:
#
#   terraform init -backend-config="bucket=vg-tfstate-prod"
#
# bootstrap-state.sh enables versioning + encryption on the bucket and server
# side encryption on the lock table, so state content is never stored in the
# clear and never silently overwritten.

# State-key decision (repo split): this is a NEW standalone repo with NO
# existing Terraform state, so the key is set to
# "videoguard-video-processing/terraform.tfstate" from the start. Renaming a
# state key AFTER state exists would orphan the old object (Terraform would
# see an empty state and try to recreate everything), so this rename is safe
# only because no state has been written yet. Do NOT change this key once a
# real apply has created state.
terraform {
  backend "s3" {
    bucket         = "vg-tfstate-REPLACE_WITH_ENV"
    key            = "videoguard-video-processing/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "vg-tfstate-lock"
    encrypt        = true
  }
}
