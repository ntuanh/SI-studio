#!/usr/bin/env bash
# Example schedule — a template for your own.
#
# Auto-run works with ANY bash script. This file exists to show the two things
# that make the tracking good rather than merely present:
#
#   1. `::step:: <name>` before each project, `::step-done:: <name> rc=$?` after.
#      That is what turns "the script is running" into "project 4 of 9, DAG,
#      failed with exit 1 after 12 minutes".
#   2. NOT aborting on the first failure, so one broken project does not cost
#      you the whole night's queue.
#
# Without markers you still get: start, finish, exit code, stall warnings, and
# a best-effort read of `=== banner ===` lines. Markers are just better.
#
# Run it:  POST /autorun/start {"script": "example-schedule.sh"}

set -uo pipefail          # NOT `set -e`: a failed project should not kill the queue

# ---------------------------------------------------------------- your projects
# Edit this list. Format:  <label>|<directory>|<command>
PROJECTS=(
  "dmsf|/home/ntuanh/DMSF/baseline-dmsf|python server.py"
  "DAG|/home/hgiang/Partitioning-DAG|python server.py"
  "standalone|/home/ntuanh/standalone-inference|python server.py"
  "PA|/home/ntuanh/split_inference_test|python server.py"
)

# Per-project wall-clock budget. A hung project is skipped instead of blocking
# every project queued behind it.
TIMEOUT="${AUTORUN_TIMEOUT:-2h}"

total=${#PROJECTS[@]}
failed=0
i=0

echo "::note:: schedule starting — ${total} projects, ${TIMEOUT} budget each"

for entry in "${PROJECTS[@]}"; do
  IFS='|' read -r label dir cmd <<< "$entry"
  i=$((i + 1))

  # This line is what the tracker reads. `[i/total]` is also understood, and
  # tells the UI how many steps to expect.
  echo "::step:: [${i}/${total}] ${label}"

  if [[ ! -d "$dir" ]]; then
    echo "::fail:: ${label}: no such directory ${dir}"
    failed=$((failed + 1))
    continue
  fi

  start=$SECONDS
  ( cd "$dir" && timeout "$TIMEOUT" bash -c "$cmd" ) 2>&1
  rc=$?
  elapsed=$((SECONDS - start))

  # rc=124 is `timeout` killing it — worth distinguishing from a real crash.
  if [[ $rc -eq 124 ]]; then
    echo "::note:: ${label} hit the ${TIMEOUT} timeout"
  fi

  # Always emit this, pass or fail: it closes the step and carries the code.
  echo "::step-done:: ${label} rc=${rc}"
  [[ $rc -ne 0 ]] && failed=$((failed + 1))

  echo "   ${label} finished in ${elapsed}s (rc=${rc})"
done

echo "::note:: schedule done — $((total - failed))/${total} ok"

# The run's own exit code is authoritative and needs no markers, so make it
# mean something: non-zero if any project failed.
exit $(( failed > 0 ? 1 : 0 ))
