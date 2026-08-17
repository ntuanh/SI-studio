# `autorun/` — schedule scripts

Put the bash scripts you want to run unattended here. `POST /autorun/start`
refuses any path outside this directory (unless `AUTORUN_ALLOW_ANY_PATH=true`),
because that endpoint executes a shell script with the control plane's own
privileges — *which file runs* is the entire security boundary.

> **You may not need a script at all.** If your projects differ only by which
> directory the same three commands run in — server, then the edges, then the
> clouds — use **▶ Run all projects** on the Progress tab instead. It runs the
> list you edit there over the Control tab's own SSH sessions, with no bash to
> write. Scripts are for the projects that need more: a config edit between
> runs, a different launch order, a validator. See "The project queue" in
> `backend/README.md`.

`runs/` is written by the service: one folder per run, holding `manifest.json`
(status, per-step timings, exit codes) and `output.log` (the full transcript).
It is gitignored. Deleting a folder deletes that run from the history — there is
no database to keep in sync.

## Quick start

```bash
# 1. one-time: tell the service where to send messages (backend/.env)
TELEGRAM_BOT_TOKEN=123456789:AA...      # from @BotFather
TELEGRAM_CHAT_ID=987654321              # from @userinfobot

# 2. verify before trusting it with an overnight run
curl -X POST localhost:8000/autorun/notify/test -H "X-API-Token: $API_TOKEN"

# 3. run a schedule
curl -X POST localhost:8000/autorun/start \
  -H "X-API-Token: $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"script": "example-schedule.sh"}'
```

`example-schedule.sh` is a working template — copy it, edit the `PROJECTS`
list, done.

## `fleet-3project.sh` — the lab schedule

Runs **split → PA → dmsf** back to back on the 13-host fleet, combining
`run/guides/{split,PA,dmsf}.md` into one script. All three use the same short
video (905 frames, `md5 3478859f21d1…`), which already exists at each project's
own path on all 9 edges — the script *verifies* it rather than copying.

```bash
DRY_RUN=1 bash autorun/fleet-3project.sh   # checks everything, launches nothing
ONLY=PA   bash autorun/fleet-3project.sh   # one project
```

Or press **▶ Run schedule** on the Progress tab, which is the same thing with a
status board attached.

| Knob | Default | What it does |
|---|---|---|
| `DRY_RUN` | `0` | preflight + project dirs + video on all 9 edges, no launches |
| `ONLY` | *(all)* | run a single project |
| `POLL_EVERY` | `20` | seconds between progress polls |
| `NOTE_EVERY` | `12` | polls per Telegram note (12 × 20 s = one every 4 min) |
| `RUN_BUDGET` | `1500` | per-project seconds before giving up (never kills) |

`fleet.py` beside it is the SSH driver — dai direct over Tailscale, every LAN
host as a `direct-tcpip` channel through dai, because that subnet is not routed
from the workstation. It reads all five credentials from the environment, so
neither file contains a password and both are safe to commit.

Per-project details worth knowing, all inherited from the runbooks: PA needs
`server.clients = [9, 9]` (with `[9, 3]` the run hangs forever) and launches
cloud before edge; dmsf needs `--device cpu` on every client because no worker
has a GPU; split launches edge before cloud. The script encodes each one's own
order rather than imposing a single one.

## Marker protocol

Any bash script works. These four lines make the tracking precise instead of
approximate:

| Marker | Meaning |
|---|---|
| `::step:: <name>` | a project started (closes the previous one as ok) |
| `::step-done:: <name> rc=<n>` | it finished; `rc` decides ok vs failed |
| `::progress:: batch=N fps=X` | live counters for the open step — **UI only** |
| `::note:: <text>` | a milestone, **forwarded to Telegram** |
| `::fail:: <text>` | fail the current step with a reason |

Emit them on stdout — `echo "::step:: DAG"` is the whole integration.

`::progress::` and `::note::` are separate on purpose. A run polling its FPS
every 20 seconds for half an hour would fire ~80 Telegram messages and train
you to ignore the channel, so progress updates the Progress tab and nothing
else; `::note::` stays the "actually tell me" marker. Anything shaped `k=v`
becomes a field (`batch`, `fps`, `total`, `reg`, `archive` are the ones the UI
renders specially); the rest is kept as free text. Supply `total=` and the row
gets a progress bar — without it there is no honest denominator, so no bar.

A `[3/12]` prefix on a step name sets the expected total, whether it appears in
a `::step::` marker or on a bare banner line.

**With no markers at all** you still get the run's start, its exit code, its
duration, stall warnings, and a full transcript. Auto-run also reads common
banner shapes (`=== name ===`, `--- name`, `### name`, `[3/12] name`) so an
existing script gets per-project tracking with no edits. Those heuristics turn
off permanently the moment a real `::step::` marker appears, so a script that
prints both is read the way its author meant.

Set `"markers": "strict"` to use explicit markers only, or `"off"` to track
just the run as a whole.

## Things worth knowing

- **One schedule at a time.** A second `POST /autorun/start` gets a 409. These
  scripts drive the whole fleet; two would contend for the same GPUs and broker
  queues.
- **`POST /autorun/stop` kills the process tree**, not just `bash` — the child
  runs in its own process group, so the `python server.py` it launched dies with
  it. It escalates SIGINT → SIGTERM → SIGKILL, so a project that writes result
  logs on the way out gets the chance to.
- **Going quiet is reported, never punished.** After `AUTORUN_STALL_SECONDS`
  (default 15 min) with no output you get a "still running, not stopped"
  message. Training steps are legitimately silent; killing one would destroy the
  run this feature exists to protect.
- **Don't use `set -e`** in a schedule. It aborts the queue on the first failing
  project, which is the opposite of what an overnight batch is for. Track
  failures in a counter and `exit 1` at the end instead — `example-schedule.sh`
  does this.
- Output also streams to the Control tab console over `/ws/stream`, so a browser
  that is open sees it live.
