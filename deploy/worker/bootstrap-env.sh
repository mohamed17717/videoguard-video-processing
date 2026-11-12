#!/usr/bin/env bash
# Boot-time configuration loader for the video-guard worker agent.
#
# The launch template (infra/asg.tf) sets:
#   user_data = base64encode(jsonencode({ VG_* = ... }))
# Nothing else reads that user data, so this script is the bridge between
# Terraform and /etc/vg/worker.env:
#
#   1. obtains an IMDSv2 token (PUT /latest/api/token, TTL header) - the AMI
#      enforces http_tokens = "required", so IMDSv1 is NOT available;
#   2. fetches /latest/user-data with that token;
#   3. base64-decodes it (launch-template user data is base64), parses the
#      JSON and validates every key against ^VG_[A-Z0-9_]+$;
#   4. atomically writes /etc/vg/worker.env: one KEY=value line per setting,
#      shell-quoted, mode 0644.
#
# It FAILS LOUDLY (non-zero, no file written) when user data is missing,
# malformed, empty or carries a bad key/value. worker-agent.service has
# Requires=vg-worker-bootstrap.service, so the agent never starts with an
# empty configuration.
#
# Runs as root from vg-worker-bootstrap.service, before worker-agent.service.
set -euo pipefail

IMDS_BASE="${VG_IMDS_BASE:-http://169.254.169.254}"
IMDS_TOKEN_TTL="${VG_IMDS_TOKEN_TTL:-60}"
ENV_FILE="${VG_ENV_FILE:-/etc/vg/worker.env}"
ENV_DIR="$(dirname "$ENV_FILE")"
KEY_PATTERN='^VG_[A-Z0-9_]+$'

log() { printf '[vg-bootstrap] %s\n' "$*"; }
die() { printf '[vg-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# Python 3 is baked into the AMI at this path (Packer step 6); fall back to
# the distro interpreter so the script can also be exercised outside the AMI.
PYTHON_BIN="${VG_BOOTSTRAP_PYTHON:-/usr/local/pyenv/versions/3.12.0/bin/python3.12}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
[ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ] || \
    die "no python3 interpreter found (set VG_BOOTSTRAP_PYTHON)"

install -d -m 0755 "$ENV_DIR"

log "requesting IMDSv2 token"
TOKEN="$(curl -fsS --connect-timeout 2 --max-time 10 -X PUT \
    -H "X-aws-ec2-metadata-token-ttl-seconds: ${IMDS_TOKEN_TTL}" \
    "${IMDS_BASE}/latest/api/token")" \
    || die "IMDSv2 token request failed; is the instance metadata service reachable?"
[ -n "$TOKEN" ] || die "IMDSv2 returned an empty token"

log "fetching launch-template user data"
USER_DATA="$(curl -fsS --connect-timeout 2 --max-time 10 \
    -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "${IMDS_BASE}/latest/user-data")" \
    || die "GET /latest/user-data failed"
[ -n "$USER_DATA" ] || \
    die "user data is empty: the launch template did not deliver VG_* settings"

TMP_FILE="$(mktemp "${ENV_DIR}/.worker.env.XXXXXX")"
trap 'rm -f "$TMP_FILE"' EXIT

log "parsing user data and validating VG_* settings"
# The payload goes through the environment, NOT stdin: the heredoc below
# already occupies the script's stdin (a `printf | python <<'PY'` pipeline
# would feed the heredoc to python and drop the payload).
VG_USER_DATA="$USER_DATA" "$PYTHON_BIN" - "$ENV_FILE" "$TMP_FILE" "$KEY_PATTERN" <<'PY'
"""Decode, validate and render launch-template user data as worker.env."""
import base64
import json
import os
import re
import shlex
import sys

env_file, tmp_file, key_pattern = sys.argv[1:4]
raw = os.environ["VG_USER_DATA"].encode("utf-8").strip()


def fail(message):
    print(f"[vg-bootstrap] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


if not raw:
    fail("user data is empty")

# Launch-template user data arrives base64-encoded. Strip whitespace first
# (some clients wrap the blob); a raw JSON document is accepted as a fallback
# so hand-written/test user data keeps working.
compact = b"".join(raw.split())
try:
    document = json.loads(base64.b64decode(compact, validate=True))
except Exception:
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"user data is neither base64-encoded JSON nor JSON: {exc}")

if not isinstance(document, dict) or not document:
    fail("user data must be a non-empty JSON object of VG_* settings")

pattern = re.compile(key_pattern)
lines = [
    "# Generated at boot by /opt/vg/bootstrap-env.sh from launch-template user data.",
    "# Do not edit: the next boot overwrites this file.",
]
for key in sorted(document):
    if not pattern.fullmatch(key):
        fail(f"invalid setting name {key!r}: expected {key_pattern}")
    value = document[key]
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = str(value)
    elif not isinstance(value, str):
        fail(f"invalid value for {key}: only scalar values are allowed")
    if not value:
        fail(f"empty value for {key}: refusing to write an unusable config")
    if "\n" in value or "\x00" in value:
        fail(f"invalid value for {key}: newlines/NUL are not allowed")
    lines.append(f"{key}={shlex.quote(value)}")

with open(tmp_file, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(tmp_file, 0o644)
os.replace(tmp_file, env_file)
print(f"wrote {len(document)} VG_* settings to {env_file}")
PY

# Python moved the temp file into place (os.replace); enforce the final mode
# and disarm the cleanup trap.
chmod 0644 "$ENV_FILE"
trap - EXIT
log "configuration written to $ENV_FILE"
