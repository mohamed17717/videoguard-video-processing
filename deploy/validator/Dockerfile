# syntax=docker/dockerfile:1
#
# Pinned validation image for the worker `validated` stage: ffprobe (built
# from the SAME pinned FFmpeg commit as the generator image) plus ClamAV with
# definitions BAKED at build time (freshclam runs at BUILD, never at
# runtime), non-root UID/GID 10001, and NO network at runtime.
#
# DEFERRED GATE (CONTRACT.md section 11): the ClamAV validator image and the
# EICAR scan are a build/CI gate. This Dockerfile is NOT built or scanned in
# this environment. It must remain syntactically valid and self-consistent
# (docker build --check will verify it in CI without executing it).
#
# Build command (in CI, after packer/ironing):
#   docker build --platform=linux/amd64 \
#     -f deploy/validator/Dockerfile -t vg-validator:pinned-<sha> \
#     --build-arg FFMPEG_COMMIT=3a0867c2bfda4a4d4309ca1a8cbdc6175e67f587 .
# IMPORTANT: the build context is the repo root (`.`), which DOES contain
# forensic-wm/ as a nested submodule in this repo. The root .dockerignore excludes
# forensic-wm/ from the context, so it is never sent to the daemon. This
# Dockerfile stays self-consistent: it builds ffprobe from the pinned FFmpeg
# git tarball URL and only COPYs a heredoc script - it never COPYs anything
# from the context, so the presence of forensic-wm/ cannot affect the image.

ARG FFMPEG_COMMIT=3a0867c2bfda4a4d4309ca1a8cbdc6175e67f587

# ---------------------------------------------------------------------------
# Stage 1: build a STATIC ffprobe from the pinned FFmpeg source. The commit
# is the same one forensic-wm/deploy/generator/Dockerfile pins, so the probe
# and the generator read the same media byte layout. Probe-only flags: no
# decoders needed beyond what avformat requires for MP4 stream inspection.
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 debian:bookworm-slim AS ffprobe-builder
ARG FFMPEG_COMMIT
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        nasm \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sources
RUN curl -fsSL "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/${FFMPEG_COMMIT}" \
        -o ffmpeg.tar.gz \
    && tar -xzf ffmpeg.tar.gz

WORKDIR /sources/FFmpeg-${FFMPEG_COMMIT}
RUN PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure \
        --prefix=/opt/ffmpeg \
        --disable-autodetect \
        --disable-doc \
        --disable-debug \
        --disable-network \
        --disable-shared \
        --enable-static \
        --disable-programs \
        --enable-ffprobe \
        --enable-demuxer=mov,mp4,m4a,3gp,3g2,mj2 \
        --enable-parser=h264,hevc,aac \
        --enable-protocol=file \
    && make -j"$(nproc)" \
    && make install

# Fail the build if the pinned binary is missing or the version drifted.
# `make install` does not copy the license files, so stage 2's COPY of
# COPYING.LGPLv2.1 needs this explicit copy from the source tree.
RUN set -eux; \
    test -x /opt/ffmpeg/bin/ffprobe; \
    /opt/ffmpeg/bin/ffprobe -version | head -n 1 | grep -q 'version 7.1.5'; \
    cp "/sources/FFmpeg-${FFMPEG_COMMIT}/COPYING.LGPLv2.1" /opt/ffmpeg/COPYING.LGPLv2.1

# ---------------------------------------------------------------------------
# Stage 2: runtime. Minimal Debian + a statically-built ffprobe + ClamAV
# binaries with virus definitions baked in at BUILD time. Nothing is updated
# at runtime: no networks, no freshclam daemon, no apt.
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 debian:bookworm-slim AS runtime
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        clamav \
        clamav-daemon \
        clamav-freshclam \
        python3-minimal \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Bake the definitions NOW (build time has network; runtime must not).
RUN freshclam --quiet \
    && test -f /var/lib/clamav/main.cvd

COPY --from=ffprobe-builder /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe
COPY --from=ffprobe-builder /opt/ffmpeg/COPYING.LGPLv2.1 /opt/ffmpeg/COPYING.LGPLv2.1

# The validation orchestration script: one JSON verdict on stdout holding
# BOTH the raw ffprobe report and the ClamAV exit status, so the worker maps
# business meaning (malware vs probe failure) on the host.
COPY <<'PYEOF' /usr/local/bin/vg-validator
#!/usr/bin/env python3
"""Run ffprobe + clamscan on the mounted input; print one verdict JSON."""
import argparse
import json
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--ffprobe-bin", default="/usr/local/bin/ffprobe")
parser.add_argument("--clamscan-bin", default="clamscan")
args = parser.parse_args()

verdict = {"ffprobe": None, "ffprobe_exit": 1, "clamscan_exit": 2, "clam_output": "", "error": ""}

try:
    probe = subprocess.run(
        [args.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", args.input],
        capture_output=True, text=True, timeout=600,
    )
    verdict["ffprobe_exit"] = probe.returncode
    if probe.returncode == 0:
        verdict["ffprobe"] = json.loads(probe.stdout)
    elif probe.stderr:
        verdict["error"] = probe.stderr[-300:]
except Exception as exc:
    verdict["error"] = f"ffprobe wrapper failed: {type(exc).__name__}"

try:
    scan = subprocess.run(
        [args.clamscan_bin, "--no-summary", "--infected", args.input],
        capture_output=True, text=True, timeout=3600,
    )
    verdict["clamscan_exit"] = scan.returncode
    verdict["clam_output"] = ((scan.stdout or "") + (scan.stderr or "")).strip()
except Exception as exc:
    verdict["clam_output"] = f"clamscan wrapper failed: {type(exc).__name__}"

print(json.dumps(verdict))
PYEOF
RUN chmod 0755 /usr/local/bin/vg-validator \
    && groupadd --gid 10001 validator \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/validator \
        --shell /usr/sbin/nologin validator \
    && mkdir -p /work/input \
    && chown -R 10001:10001 /work /home/validator \
    && /usr/local/bin/vg-validator --input /dev/null >/dev/null

# Hardened runtime configuration (flags themselves come from the worker argv):
# no network capability is possible at runtime because the worker ALWAYS passes
# --network=none; this image carries nothing that would need it.
USER 10001:10001
WORKDIR /work
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/vg-validator"]
