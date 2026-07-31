#!/usr/bin/env bash
# Install the agent's runtime dependencies on a device (guide §9).
# The orchestrator scp's this alongside the agents and runs it when
# /run/deploy is called with install_deps=true.
#
#   bash /opt/split-inference/agent/bootstrap.sh
#
# Idempotent: skips anything already importable. Torch is NOT installed
# automatically on Jetson -- those boards need NVIDIA's own wheels, so the
# script reports what to do instead of installing something that won't work.

set -euo pipefail

ROOT="${SPLIT_INFERENCE_ROOT:-/opt/split-inference}"
PY="${REMOTE_PYTHON:-python3}"

log() { printf '[bootstrap] %s\n' "$*"; }

mkdir -p "$ROOT/models" "$ROOT/logs" "$ROOT/run" "$ROOT/agent"

log "python: $($PY --version 2>&1)"

have() { $PY -c "import $1" >/dev/null 2>&1; }

PIP="$PY -m pip"
if ! $PY -m pip --version >/dev/null 2>&1; then
  log "pip missing; attempting ensurepip"
  $PY -m ensurepip --upgrade || {
    log "ERROR: no pip available. Install python3-pip and re-run."
    exit 1
  }
fi

# --- always-needed, small, pure-wheel dependencies ---
for pkg in pika numpy; do
  mod="$pkg"
  if have "$mod"; then
    log "$pkg already present"
  else
    log "installing $pkg"
    $PIP install --user --no-input "$pkg"
  fi
done

# --- torch: platform-sensitive, so detect before touching it ---
if have torch; then
  log "torch present: $($PY -c 'import torch; print(torch.__version__, "cuda", torch.cuda.is_available())')"
else
  if [ -f /etc/nv_tegra_release ] || uname -r | grep -qi tegra; then
    log "ERROR: Jetson/Tegra detected and torch is missing."
    log "       Install NVIDIA's JetPack wheel manually -- pip's torch has no CUDA"
    log "       support for Tegra. See:"
    log "       https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/"
    exit 1
  fi
  log "installing torch (CPU/CUDA wheel from PyPI defaults)"
  $PIP install --user --no-input torch
fi

# --- optional: real camera / video frame sources on the edge ---
if have cv2; then
  log "opencv present"
else
  log "installing opencv-python-headless (optional: video/image frame sources)"
  $PIP install --user --no-input opencv-python-headless || \
    log "WARN: opencv install failed; agent falls back to synthetic frames"
fi

# --- optional: ultralytics, only if you plan to re-split models on-device ---
if have ultralytics; then
  log "ultralytics present"
else
  log "skipping ultralytics (not needed at runtime -- shards are TorchScript)"
fi

log "verifying agent imports"
$PY -c "
import sys; sys.path.insert(0, '$ROOT/agent')
import codec, numpy, pika, torch
print('ok', torch.__version__, 'cuda', torch.cuda.is_available())
"

log "done. shards go in $ROOT/models/{head.pt,tail.pt}"
