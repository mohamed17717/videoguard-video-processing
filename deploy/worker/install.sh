#!/usr/bin/env bash
# AMI-provisioning script for the worker agent (run as root by Packer).
#
# What it does (CONTRACT.md sections 5, 8):
#   1. builds a venv on the pinned Python 3.12 and installs vg_pipeline +
#      vg_worker from this checkout plus requirements-worker.txt;
#   2. creates the non-root service user `vgworker` with UID/GID 10001 —
#      the SAME uid/gid the generator/validator containers run as, so the
#      0400 secret file and the /work output tree are usable without a
#      root chown — and adds it to the `docker`
#      group so the docker CLI (never a mounted socket) works;
#   3. writes /etc/vg/worker.yaml (from configs/aws.worker.yaml) and the
#      /etc/vg/worker.env skeleton; at boot the launch template's user_data
#      is loaded by /opt/vg/bootstrap-env.sh, which atomically overwrites
#      that file with the real VG_* values (real ARNs belong at deploy time,
#      NOT in this repo);
#   4. prepares /work (100 GiB gp3 data volume per launch template), the
#      tmpfs /run/vg-secrets, the docker CLI config dir and the passwordless
#      `shutdown` grant the ASG-termination fallback needs;
#   5. installs + enables the bootstrap unit and the worker agent unit.
#
# DEFERRED GATE: the Packer build is not run here; this script ships inside
# the AMI provisioning hook. Variables like VG_S3_BUCKET are passed in by
# Packer at build time or instance user-data, never stored in git.
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/vg}"
REPO_ROOT="${REPO_ROOT:?REPO_ROOT must point at the video-processing checkout}"
VG_USER="vgworker"
VG_UID="${VG_UID:-10001}"
VG_GID="${VG_GID:-10001}"
PYTHON_BIN="${PYTHON_BIN:-/usr/local/pyenv/versions/3.12.0/bin/python3.12}"

log() { printf '[install] %s\n' "$*"; }

test -x "$PYTHON_BIN" || { echo "pinned python3.12 missing at $PYTHON_BIN" >&2; exit 1; }

# ---------------------------------------------------------------- 1. venv --
log "building the application venv at $APP_ROOT/venv"
"$PYTHON_BIN" -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/python3.12" -m pip install --upgrade pip
"$APP_ROOT/venv/bin/python3.12" -m pip install \
    --require-virtualenv \
    -r "$REPO_ROOT/requirements-worker.txt"
"$APP_ROOT/venv/bin/python3.12" -m pip install \
    --no-deps \
    --require-virtualenv \
    "$APP_ROOT/packages/vg_pipeline" "$APP_ROOT/packages/vg_worker" \
    || "$APP_ROOT/venv/bin/python3.12" -m pip install \
        --no-deps \
        --require-virtualenv \
        -e "$REPO_ROOT"
"$APP_ROOT/venv/bin/python3.12" - <<'PY'
import vg_pipeline, vg_worker  # noqa: F401  (fail the install if either import misses)
print("imports ok:", vg_pipeline.__version__, vg_worker.__version__)
PY

# ------------------------------------------------------- 2. service user --
if ! id -u "$VG_USER" >/dev/null 2>&1; then
    groupadd --gid "$VG_GID" "$VG_USER"
    useradd --uid "$VG_UID" --gid "$VG_GID" --create-home \
        --home-dir /var/lib/"$VG_USER" --shell /usr/sbin/nologin "$VG_USER"
fi
# Non-root CLI access to docker (daemon-side socket group), per contract 8.
if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$VG_USER"
else
    log "WARNING: no docker group; the ASG launch must install docker before the unit"
fi

# ------------------------------------------------- 3. config + env file --
install -d -m 0755 /etc/vg
install -d -o "$VG_USER" -g "$VG_USER" -m 0750 "$APP_ROOT" /var/lib/vgworker
# Docker CLI config dir: entrypoint.sh runs `docker login` at boot with
# DOCKER_CONFIG pointing here (worker-agent.service), so the credential file
# stays in the vgworker-owned state dir under ProtectSystem=strict.
install -d -o "$VG_USER" -g "$VG_USER" -m 0700 /var/lib/vgworker/.docker
install -m 0644 "$REPO_ROOT/configs/pipeline.yaml"     /etc/vg/pipeline.yaml
install -m 0644 "$REPO_ROOT/configs/aws.worker.yaml"   /etc/vg/worker.yaml
install -m 0755 "$REPO_ROOT/deploy/worker/entrypoint.sh" "$APP_ROOT/venv/bin/entrypoint-worker.sh"
install -m 0755 "$REPO_ROOT/deploy/worker/bootstrap-env.sh" "$APP_ROOT/bootstrap-env.sh"

# worker.env skeleton: only VG_* NAMES, no secrets. It is a FALLBACK so a
# manual run has variables defined; at boot vg-worker-bootstrap.service
# OVERWRITES it with the launch template's user_data and fails loudly if
# that data is missing (the agent unit Requires= the loader).
cat >/etc/vg/worker.env <<'EOF'
# Skeleton; the boot loader replaces the values from launch-template user data.
VG_AWS_REGION=
VG_S3_BUCKET=
EOF
chmod 0644 /etc/vg/worker.env

# ------------------------------------------------------------ 4. volumes --
# /work exists per launch template (100 GiB gp3 encrypted, delete-on-termination).
install -d -m 0750 -o "$VG_USER" -g "$VG_USER" /work
# tmpfs for the watermark secret: 64 KiB+ is plenty; mounted read-write by
# the AGENT only (write + unlink), mounted READ-ONLY into the container.
if ! findmnt --mountpoint /run/vg-secrets >/dev/null 2>&1; then
    install -d -m 0750 -o "$VG_USER" -g "$VG_USER" /run/vg-secrets
    grep -q vg-secrets /etc/fstab 2>/dev/null || \
        printf 'tmpfs /run/vg-secrets tmpfs size=1m,mode=0750,uid=%s,gid=%s,noexec,nosuid,nodev 0 0\n' \
            "$VG_UID" "$VG_GID" >>/etc/fstab
    mount /run/vg-secrets 2>/dev/null || true
fi
# The `failed-to-terminate` fallback needs a passwordless halt (agent is
# non-root). Narrow grant: only the exact shutdown command.
SUDOERS=/etc/sudoers.d/vgworker-shutdown
printf '%s ALL=(root) NOPASSWD: /sbin/shutdown -h now\n' "$VG_USER" >"$SUDOERS"
chmod 0440 "$SUDOERS"

# -------------------------------------------------------- 5. systemd units --
# Order: the one-shot loader runs first (Before=worker-agent.service) and
# writes /etc/vg/worker.env from launch-template user data; the agent unit
# Requires= it, so a loader failure blocks the agent instead of starting an
# empty configuration.
install -m 0644 "$REPO_ROOT/deploy/worker/vg-worker-bootstrap.service" /etc/systemd/system/vg-worker-bootstrap.service
install -m 0644 "$REPO_ROOT/deploy/worker/worker-agent.service" /etc/systemd/system/worker-agent.service
systemctl daemon-reload
systemctl enable vg-worker-bootstrap.service
systemctl enable worker-agent.service
# The ASG bootstraps units automatically once the instance is running; the
# worker agent is NOT left enabled-running here because one boot = one job,
# driven by the launch template's user-data triggering `systemctl start`.

log "worker agent installed; unit enabled (start happens at boot)"
