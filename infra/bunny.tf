# Bunny.net pull zone, managed via null_resource + curl.
#
# WHY NOT A BUNNY PROVIDER: the Terraform registry has community Bunny
# providers, but none we can trust to cover pull zones (with AWS signing,
# Origin Shield and cache rules). Rather than invent resources against an
# unverified provider, this file calls the Bunny REST API
# (https://api.bunny.net) with curl through null_resource local-exec, keyed
# by the sensitive var.bunny_api_key.
#
# D2 OWNERSHIP SPLIT:
#   The edge script (create + VG_EDGE_PARAMS + publish) is owned by
#   .github/workflows/aws-pipeline.yml job `edge`; Terraform deliberately
#   does NOT manage it (ordering: CI runs on push, terraform-apply is a
#   manual dispatch). The edge token secret and origin host therefore live
#   only in GitHub secrets/vars (EDGE_TOKEN_SECRET / EDGE_ORIGIN_HOST) and
#   never in tfvars or Terraform state.
#
# What is and is NOT in Terraform state:
#   - the pull-zone payload JSON (below) IS hashed into the pull-zone
#     resource's triggers, so an apply re-runs the API call when the desired
#     config changes;
#   - the API key NEVER enters state: it is only interpolated into the
#     one-shot command, and local-exec output goes to the operator's console;
#   - Bunny's AWS origin credentials (access key id/secret of the
#     vg-bunny-origin-<env> IAM user from iam.tf) are NOT managed here. Set
#     them in the Bunny dashboard under the zone's AWS signing settings
#     (outputs.tf prints the access key id as a sensitive value for that).
#
# DEFERRED GATES (CONTRACT.md section 11):
#   - the exact Bunny API field names (pull zone) and the EdgeRule
#     ActionType enum must be verified against
#     https://docs.bunny.net/reference/ before the first real apply (the
#     payload below follows the documented pull-zone shape as of Nov 2026);
#     this INCLUDES the CORS fields EnableCORS / CorsAllowedOrigins added
#     for personalised-manifest CORS - their exact names and accepted
#     values are unverified (no real Bunny API call has been made);
#   - the EdgeScriptId attachment of the edge script to THIS zone is done
#     by CI job "edge" (aws-pipeline.yml) right after the script publish -
#     its exact endpoint/verb/field shape is likewise a deferred gate, and
#     the zone payload here deliberately does NOT pin a script id (CI owns
#     the script and only CI knows its numeric Id);
#   - actually DEPLOYING to a real Bunny account (CI job "edge", gated on
#     vars.EDGE_DEPLOY_ENABLED = 'true' + the production environment) and
#     the real edge runtime behaviour remain deferred runtime gates.
#
# Leaving var.bunny_api_key empty makes the local-exec call SKIP with a
# printed notice, so plans and applies without Bunny credentials do not
# fail. The terraform-apply CI job guards against this with `test -n`
# checks so the skip can never happen silently in a real deploy.

locals {
  bunny_zone_name = "vg-${var.env}-playback"

  # The pull zone origin path is renditions/ ONLY (CONTRACT.md section 1):
  # the CDN can never fetch the original upload, the masters, the reports or
  # the job sidecar, regardless of any URL guessed by a viewer.
  bunny_origin_url = "https://s3.${local.region}.amazonaws.com/${local.bucket_name}/renditions"

  # Pull-zone payload. Key decisions (PRICING.md, CONTRACT.md section 7):
  #   AWSSigningEnabled        - Bunny signs S3 GETs with the operator-set
  #                              origin credentials (see above).
  #   OriginShield.Enabled     - bounds origin fills to ~one per rendition
  #                              per cache window (free feature).
  #   CacheControlMaxAgeOverride - segments are immutable
  #                              (public, max-age=31536000).
  #   EdgeRules (Cache Bypass on *.m3u8 / *.mpd) - manifests are personalised
  #                              per session at the edge and must be
  #                              private, no-store. Two rules, one per
  #                              extension, because the trigger pattern is
  #                              a single pattern per rule.
  #   EnableCORS + CorsAllowedOrigins - the edge script answers CORS on the
  #                              personalised manifests it generates; these
  #                              zone fields cover the objects the script
  #                              does not touch (segments, init, thumbnails)
  #                              so browser players can range-request them
  #                              cross-origin. DEFERRED GATE: the exact
  #                              field names are unverified (see the block
  #                              comment at the top of this file).
  bunny_pull_zone_payload = jsonencode({
    Name                              = local.bunny_zone_name
    OriginUrl                         = local.bunny_origin_url
    AWSSigningEnabled                 = true
    AWSSigningRegion                  = local.region
    AWSSigningService                 = "s3"
    OriginShield                      = { Enabled = true, ShieldZoneCode = "US" }
    CacheControlMaxAgeOverride        = 31536000
    CacheControlBrowserMaxAgeOverride = 31536000
    EnableCORS                        = true
    CorsAllowedOrigins                = ["*"]
    EdgeRules = [
      {
        Description        = "Bypass cache for HLS manifests (personalised per session)"
        Enabled            = true
        ActionType         = 2
        TriggerMatchesType = 0
        TriggerPattern     = "*.m3u8"
      },
      {
        Description        = "Bypass cache for DASH manifests (personalised per session)"
        Enabled            = true
        ActionType         = 2
        TriggerMatchesType = 0
        TriggerPattern     = "*.mpd"
      },
    ]
  })
}

# The pull zone itself. Created on apply; updates re-run because the payload
# hash is a trigger. (Bunny's create API is used here; a real deployment
# should switch to the update API once the zone id is known - see README.)
resource "null_resource" "bunny_pull_zone" {
  triggers = {
    zone_name    = local.bunny_zone_name
    payload_hash = sha256(local.bunny_pull_zone_payload)
  }

  provisioner "local-exec" {
    command = "if test -n '${var.bunny_api_key}'; then curl -fsS -X POST 'https://api.bunny.net/pullzone' -H 'AccessKey: ${var.bunny_api_key}' -H 'Content-Type: application/json' -d '${local.bunny_pull_zone_payload}'; else echo 'SKIPPED: var.bunny_api_key is empty - the Bunny pull zone is NOT managed by this apply (infra/README.md).'; fi"
  }
}
