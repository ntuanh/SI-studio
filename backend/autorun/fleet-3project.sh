#!/usr/bin/env bash
# =============================================================================
# Three projects, one queue: split -> PA -> dmsf, on the 13-host lab fleet.
#
# Combines run/guides/{split,PA,dmsf}.md into one unattended schedule. Each
# project runs the SAME short video, already present on all 9 edges as
#   ~/ntuanh/Optimizer/split_inference_test/video.mp4
#   (8,268,922 B, md5 3478859f21d1163feb532fbf526d65fc, 905 frames @ 29.97fps)
# and byte-identical at PA's and dmsf's own paths -- verified, not copied.
#
# Runs FROM the workstation: the 192.168.101.0/24 lab subnet is not routed here,
# so every LAN session tunnels through dai (autorun/fleet.py). No credential is
# in this file -- they come from backend/.env, which is why this one is safe to
# commit and the runbooks are not.
#
# Progress markers this emits, read by app/services/autorun.py:
#   ::step:: / ::step-done:: rc=N   per project, drives pass/fail + Telegram
#   ::progress:: batch=N fps=X      live counters, UI only (never notified)
#   ::note:: text                   milestones, DOES go to Telegram (kept rare)
#
# Deliberately NOT `set -e`: one project failing must not cancel the queue.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_PY="${FLEET_PYTHON:-d:/SplitInference/venv/Scripts/python.exe}"
FLEET="$HERE/fleet.py"

# Which projects to run, and how long to allow each. PA is the long one
# (~8-9 min of run plus registration); dmsf reference is ~4.5 min.
ONLY="${ONLY:-}"                       # e.g. ONLY=PA to run one project
POLL_EVERY="${POLL_EVERY:-20}"         # seconds between progress polls
NOTE_EVERY="${NOTE_EVERY:-12}"         # polls per Telegram ::note:: (12*20s = 4min)
RUN_BUDGET="${RUN_BUDGET:-1500}"       # per-project seconds before giving up

# DRY_RUN=1 exercises everything that is cheap and reversible -- preflight, the
# project directories, the video on all 9 edges, the stragglers sweep, and every
# marker this emits -- without starting a single server or client. It is how you
# check the plumbing at 5pm without committing the fleet to half an hour.
DRY_RUN="${DRY_RUN:-0}"

fleet() { PYTHONIOENCODING=utf-8 "$FLEET_PY" "$FLEET" "$@"; }

# --------------------------------------------------------------------- helpers
say()      { echo "$*"; }
progress() { echo "::progress:: $*"; }
note()     { echo "::note:: $*"; }

# One project's checks, without launching anything. Returns the same rc a real
# run would for the failures it can actually see.
dry_project() {
  local label="$1" dir="$2" video="$3"
  say "   DRY RUN — checking only, launching nothing"
  local out
  out=$(fleet run dai "test -d \$HOME/$dir && test -f \$HOME/$dir/server.py && echo PROJECT_OK || echo PROJECT_BAD" 90 2>&1)
  echo "$out" | grep -q PROJECT_OK || { echo "::fail:: $label: $dir has no server.py on dai"; return 1; }
  say "   project dir + server.py present on dai"
  verify_video "$video" || return 1
  progress "project=$label batch=0 fps=0 dry=1"
  say "   would start: server on dai, then 12 clients"
  return 0
}

# Kill anything left from a previous project. Between projects this is not
# optional: two servers on dai would both bind the same rpc_queue and the
# second run's clients would register into the first one's topology.
kill_fleet() {
  say "   cleaning stragglers across the fleet"
  fleet fanout all 'pkill -f "client.py --layer_id" 2>/dev/null; pkill -f "server.py" 2>/dev/null; sleep 1; echo "left=$(pgrep -fc "client.py --layer_id|server.py" 2>/dev/null || echo 0)"' 120 \
    2>&1 | grep -E "^=====|left=" | sed 's/^/     /'
}

# The video is the run's input; if an edge lost it the project silently produces
# nothing, so check before spending 9 minutes finding out.
verify_video() {
  local rel="$1"
  local missing
  missing=$(fleet fanout edge "test -f \$HOME/$rel && echo ok || echo MISSING" 90 2>&1 | grep -c MISSING)
  if [ "$missing" -gt 0 ]; then
    echo "::fail:: $missing edge(s) are missing $rel"
    return 1
  fi
  say "   video present on all 9 edges: $rel"
  return 0
}

# Poll dai's server log until the server exits or the budget runs out.
# $1 project label, $2 project dir, $3 server log, $4 command producing
# "batch|fps|extra" from inside the project dir.
watch_run() {
  local label="$1" dir="$2" log="$3" probe="$4"
  local waited=0 ticks=0 last_batch=-1 stall=0

  while [ "$waited" -lt "$RUN_BUDGET" ]; do
    sleep "$POLL_EVERY"
    waited=$((waited + POLL_EVERY)); ticks=$((ticks + 1))

    local out batch fps extra alive
    out=$(fleet run dai "cd \$HOME/$dir 2>/dev/null && { $probe ; }; echo \"alive=\$(pgrep -fc 'server.py' 2>/dev/null || echo 0)\"" 90 2>&1)
    batch=$(echo "$out" | sed -n 's/^BATCH=//p' | tail -1)
    fps=$(echo   "$out" | sed -n 's/^FPS=//p'   | tail -1)
    extra=$(echo "$out" | sed -n 's/^EXTRA=//p' | tail -1)
    alive=$(echo "$out" | sed -n 's/^alive=//p' | tail -1)

    progress "project=$label batch=${batch:-0} fps=${fps:-0} elapsed=${waited}s ${extra:-}"

    # A Telegram note only every NOTE_EVERY polls -- the UI gets every tick.
    if [ $((ticks % NOTE_EVERY)) -eq 0 ]; then
      note "[$label] batch ${batch:-0} · ${fps:-0} fps · ${waited}s elapsed"
    fi

    if [ "${alive:-0}" = "0" ]; then
      say "   server exited after ${waited}s"
      return 0
    fi
    # Report a wedged run, but never kill it: the archive is written at
    # shutdown, so killing here would destroy the results (PA.md 6, dmsf.md 9).
    if [ "${batch:-0}" = "$last_batch" ]; then
      stall=$((stall + 1))
      [ "$stall" -eq 15 ] && say "   ! batch counter flat for $((stall * POLL_EVERY))s (not killing)"
    else
      stall=0; last_batch="${batch:-0}"
    fi
  done

  say "   ! budget ${RUN_BUDGET}s exhausted; leaving the run alone"
  return 2
}

# ============================================================== 1 · split
run_split() {
  local DIR="ntuanh/Optimizer/split_inference_test"
  echo "::step:: [1/3] split"
  if [ "$DRY_RUN" = "1" ]; then dry_project split "$DIR" "$DIR/video.mp4"; return $?; fi
  kill_fleet
  verify_video "$DIR/video.mp4" || return 1

  say "   starting server on dai"
  fleet launch dai "cd \$HOME/$DIR && rm -f server.log && setsid nohup python3 -u server.py > server.log 2>&1 < /dev/null" >/dev/null
  sleep 8
  fleet run dai "tail -3 \$HOME/$DIR/server.log" 60 2>&1 | sed 's/^/     /'

  # split.md 5: edges first, then clouds.
  say "   starting 9 edge clients (--layer_id 1)"
  fleet fanout edge "cd \$HOME/$DIR && rm -f client.log && setsid nohup python3 -u client.py --layer_id 1 --name \$(hostname) > client.log 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  say "   starting 3 cloud clients (--layer_id 2)"
  fleet fanout cloud "cd \$HOME/$DIR && rm -f client.log && setsid nohup python3 -u client.py --layer_id 2 --name \$(hostname) > client.log 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  note "[split] 12 clients launched, waiting for the drain"

  watch_run split "$DIR" server.log \
    'echo "BATCH=$(wc -l < batch_done_ns.log 2>/dev/null || echo 0)"; echo "FPS=$(tail -1 fps_cluster.log 2>/dev/null | grep -oE "[0-9]+\.[0-9]+" | tail -1)"; echo "EXTRA=reg=$(grep -c REGISTER server.log 2>/dev/null)"'
  local rc=$?

  validate "$DIR" "guide/validate_results.py" "" && return 0
  return "${rc:-1}"
}

# ================================================================= 2 · PA
run_pa() {
  local DIR="ntuanh/split_inference_test"
  echo "::step:: [2/3] PA"
  if [ "$DRY_RUN" = "1" ]; then dry_project PA "$DIR" "$DIR/video.mp4"; return $?; fi
  kill_fleet
  verify_video "$DIR/video.mp4" || return 1

  # PA.md 4.2: server.clients MUST be [9, 9] -- 9 layer-1 + 9 layer-2. With
  # [9, 3] the STOP expectation never matches the 18 clients that report and
  # the run hangs until killed.
  say "   ensuring server.clients = [9, 9]"
  fleet run dai "cd \$HOME/$DIR && sed -i 's/^\\(\\s*\\)- 3 #cloud/\\1- 9 #cloud/' config.yaml && sed -n '2,8p' config.yaml" 90 2>&1 | sed 's/^/     /'
  fleet fanout lan "cd \$HOME/$DIR && sed -i 's/^\\(\\s*\\)- 3 #cloud/\\1- 9 #cloud/' config.yaml" 120 >/dev/null

  say "   starting server on dai"
  fleet launch dai "cd \$HOME/$DIR && rm -f run_server.out && setsid nohup python3 -u server.py > run_server.out 2>&1 < /dev/null" >/dev/null
  sleep 8
  fleet run dai "tail -3 \$HOME/$DIR/run_server.out" 60 2>&1 | sed 's/^/     /'

  # PA.md 4.4: cloud before edge.
  say "   starting cloud clients (--layer_id 2; device-1 carries 7 threads)"
  fleet fanout cloud "cd \$HOME/$DIR && rm -f run_client.out && setsid nohup python3 -u client.py --layer_id 2 --name \$(hostname) > run_client.out 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  say "   starting 9 edge clients (--layer_id 1)"
  fleet fanout edge "cd \$HOME/$DIR && rm -f run_client.out && setsid nohup python3 -u client.py --layer_id 1 --name \$(hostname) > run_client.out 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  note "[PA] 18 clients launched (9 edge + 9 cloud), expect ~8-9 min"

  watch_run PA "$DIR" run_server.out \
    'echo "BATCH=$(wc -l < batch_done_ns.log 2>/dev/null || echo 0)"; echo "FPS=$(grep -oE "[0-9]+\.[0-9]+ fps" run_server.out 2>/dev/null | tail -1 | grep -oE "[0-9]+\.[0-9]+")"; echo "EXTRA=reg=$(grep -c "Received REGISTER" run_server.out 2>/dev/null)/18"'
  local rc=$?

  validate "$DIR" "guide/validate_results.py" "--names cluster" && return 0
  return "${rc:-1}"
}

# =============================================================== 3 · dmsf
run_dmsf() {
  local DIR="manh224353/split_inference"
  echo "::step:: [3/3] dmsf"
  if [ "$DRY_RUN" = "1" ]; then dry_project dmsf "$DIR" "$DIR/videos/video.mp4"; return $?; fi
  kill_fleet
  verify_video "$DIR/videos/video.mp4" || return 1

  say "   starting server on dai"
  fleet launch dai "cd \$HOME/$DIR && rm -f run-server.log && setsid nohup python3 -u server.py > run-server.log 2>&1 < /dev/null" >/dev/null
  sleep 8
  # The [BrokerRAM] line proves the queue-host login is right; "falling back to
  # the management API" means broker_ram is wrong (dmsf.md 7.1).
  fleet run dai "tail -5 \$HOME/$DIR/run-server.log" 60 2>&1 | sed 's/^/     /'

  # dmsf.md 7: clouds before edges. --device cpu is mandatory: no worker has a GPU.
  say "   starting 3 cloud clients (--layer_id 2 --device cpu)"
  fleet fanout cloud "cd \$HOME/$DIR && rm -f run-client.log && setsid nohup python3 -u client.py --layer_id 2 --name \$(hostname) --device cpu > run-client.log 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  say "   starting 9 edge clients (--layer_id 1 --device cpu)"
  fleet fanout edge "cd \$HOME/$DIR && rm -f run-client.log && setsid nohup python3 -u client.py --layer_id 1 --name \$(hostname) --device cpu > run-client.log 2>&1 < /dev/null & sleep 2; exit 0" 180 >/dev/null
  note "[dmsf] 12 clients launched, expect ~4-5 min"

  # dmsf.md 7.4: [FPS] DONE only starts once the W=16 window fills, so the
  # count is units-15. Not a loss.
  watch_run dmsf "$DIR" run-server.log \
    'echo "BATCH=$(grep -c "\[FPS\] DONE" run-server.log 2>/dev/null)"; echo "FPS=$(grep -oE "[0-9]+\.[0-9]+ fps" run-server.log 2>/dev/null | tail -1 | grep -oE "[0-9]+\.[0-9]+")"; echo "EXTRA=reg=$(grep -c REGISTER run-server.log 2>/dev/null)/12"'
  local rc=$?

  validate "$DIR" "baseline_DMSF/guide/validate_results.py" "--names cluster" && return 0
  return "${rc:-1}"
}

# ------------------------------------------------------------------ validate
# The archive is the deliverable; a run that produced no conformant archive did
# not succeed however cleanly its processes exited.
validate() {
  local dir="$1" validator="$2" flags="$3"
  say "   validating the archive"
  local out
  out=$(fleet run dai "cd \$HOME/$dir && D=\$(ls -1dt results/results_* 2>/dev/null | head -1) && echo \"ARCHIVE=\$D\" && python3 $validator \"\$D\" $flags 2>&1 | tail -6; echo \"VRC=\$?\"" 180 2>&1)
  echo "$out" | sed 's/^/     /'
  local archive vrc
  archive=$(echo "$out" | sed -n 's/^ *ARCHIVE=//p' | tail -1)
  vrc=$(echo "$out" | sed -n 's/^ *VRC=//p' | tail -1)
  if [ -z "$archive" ]; then
    echo "::fail:: no results archive was written"
    return 1
  fi
  progress "archive=$(basename "$archive")"
  [ "${vrc:-1}" = "0" ]
}

# ==================================================================== driver
say "::note:: fleet schedule starting — split → PA → dmsf, one short video (905 frames)"
say "workstation → dai (${FLEET_DAI_HOST:-100.68.127.89}) → 9 edge + 3 cloud"

if ! fleet check >/dev/null 2>&1; then
  echo "::fail:: fleet preflight failed — cannot reach dai / machine-2 / device-1"
  note "❌ fleet unreachable; schedule aborted before starting"
  exit 1
fi
say "preflight ok: dai, machine-2, device-1 all answered"

failed=0
total=0
for proj in split PA dmsf; do
  if [ -n "$ONLY" ] && [ "$ONLY" != "$proj" ]; then continue; fi
  total=$((total + 1))
  start=$SECONDS
  case "$proj" in
    split) run_split ;;
    PA)    run_pa    ;;
    dmsf)  run_dmsf  ;;
  esac
  rc=$?
  echo "::step-done:: $proj rc=$rc"
  say "   $proj finished in $((SECONDS - start))s (rc=$rc)"
  [ "$rc" -ne 0 ] && failed=$((failed + 1))
done

[ "$DRY_RUN" = "1" ] || kill_fleet
say "::note:: schedule done — $((total - failed))/${total} projects ok"
exit $(( failed > 0 ? 1 : 0 ))
