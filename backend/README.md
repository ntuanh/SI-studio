# Split Inference Control Plane

The tracking system behind the **Split Inference Simulation Platform** UI
(`../split-inference-pipeline.html`): one FastAPI service that **serves the
website** and runs the machinery under it — SSH fan-out to edge/cloud machines,
model-shard deployment over SCP, split inference across RabbitMQ, and live
metrics streamed back over a WebSocket.

Built to `guide_code.md`. FastAPI + asyncssh + aio-pika + SQLModel, `async`
throughout, Python 3.11+.

---

## Quick start

### With Docker (brings up RabbitMQ too)

```bash
cp .env.example .env          # set API_TOKEN at minimum
docker compose up --build
```

Open <http://localhost:8000> — that is the tracking UI, already pointed at its
own backend. API docs at `/docs`, RabbitMQ management on
<http://localhost:15672>.

> Set `DEVICE_BROKER_URL` to an address your **devices** can reach. Inside
> compose the backend talks to `amqp://rabbitmq:5672`, but the agents on remote
> machines cannot resolve that name — give them the host's routable IP, e.g.
> `DEVICE_BROKER_URL=amqp://guest:guest@192.168.1.20:5672/`.

### Locally

```bash
python -m venv .venv && . .venv/Scripts/activate    # Linux/macOS: . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python tools/build_web.py                           # build the website
docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management-alpine
uvicorn app.main:app --reload
```

SQLite tables are created on boot. Verify:

```bash
curl localhost:8000/health
# {"status":"ok","broker_connected":true,"ssh_sessions":0,"active_runs":0,"ws_subscribers":0}
```

---

## The website

`http://localhost:8000/` serves the full UI — Stages, Pipeline, Config,
Clusters, Simulation, Control — with a **Simulate / Live** toggle in the header.

| | Simulate | Live |
|---|---|---|
| Metrics | in-browser math, unchanged | `/metrics/latest` + `metrics` frames |
| Control tab | canned output | real SSH fan-out over `/control/*` |
| Run & animate flow | animation only | `POST /run/start` |
| Inventory | browser only | pushed to the backend on every sync |

Switching to **Live** pushes the current stages/devices to the backend
(`POST /seed`), fetches a metrics snapshot, and opens `/ws/stream`. The header
chip shows the connection: grey→green dot, `backend ready` → `streaming`.
Click it to change the backend URL or paste an API token.

The token is handed to the page automatically when the browser is on the same
machine (`WEB_AUTOFILL_TOKEN`, same loopback rule as `/docs`), so a local
`uvicorn` is usable immediately. A remote browser gets a blank one and is asked
to paste it — binding to `0.0.0.0` never leaks the token to visitors.

### How it is built

The UI ships as a *self-extracting bundle*: the real page is a JSON string in
`<script type="__bundler/template">`, its assets are base64 blobs, and the
Control tab's handlers are `setTimeout` mocks. `tools/build_web.py` unpacks it
into `web/` and patches it on the way through:

```bash
python tools/build_web.py            # -> backend/web/
python tools/build_web.py --check    # non-zero if the bundle changed since
```

```
web/index.html          the page, asset URLs rewritten, both patches applied
web/vendor/*.js         react, react-dom, dc-runtime  (no CDN at runtime)
web/assets/*            images
web/backend-client.js   the transport layer, copied from ui/
```

Patches: a header group (Live toggle, connection chip, Deploy), a Connect button
in the per-device ⚙ form, a **⟳ measure** button in each stage's header, and
[`ui/live-patch.js`](ui/live-patch.js) appended to the page's logic script,
which overrides `Component.prototype` — `simCluster`, `sshRun`, `sshScp`,
`sshConnectAll`, `sshServerTest`, `runFlow`. Overriding rather than rewriting
method bodies is what lets you re-export the UI from the design tool and just
re-run the build; if an anchor moves, the build **fails** instead of quietly
producing a site with a dead toggle.

`split-inference-pipeline.html` is never modified — it stays the source of
truth. To embed the client in a page of your own instead, see
[`ui/WIRING.md`](ui/WIRING.md).

### Load a scenario without the UI

`POST /seed` takes the UI's **JSON** export verbatim:

```bash
curl -X POST localhost:8000/seed \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d @split_inference_pipeline.json

curl localhost:8000/metrics/latest -H "Authorization: Bearer $API_TOKEN"
```

---

## API

Everything except `/health` and `/docs` needs `Authorization: Bearer $API_TOKEN`
(or `X-API-Token: $API_TOKEN`). The WebSocket takes `?token=` instead, because
browsers cannot set headers on a WS handshake.

**`/docs` authorizes itself.** Open <http://127.0.0.1:8000/docs> and every
endpoint is immediately usable — a green banner confirms it. The token is
injected into the page only for **loopback** requests, so serving with
`--host 0.0.0.0` still shows remote visitors the stock Authorize button rather
than your token. The check reads the socket address, not `X-Forwarded-For`, so a
remote caller cannot claim to be local. Set `DOCS_AUTOFILL_TOKEN=false` to always
authorize by hand.

| Method | Path | Purpose |
|---|---|---|
| GET | `/devices` | list inventory |
| POST | `/devices` | register a device |
| PATCH | `/devices/{id}` | edit specs / connection |
| DELETE | `/devices/{id}` | remove (closes its session, forgets its password) |
| POST | `/devices/{id}/probe` | SSH in and fill `gflops` / `bandwidth_mb_s` / `latency_ms` |
| POST | `/devices/{id}/measure` | the accurate version of the same job — see [Auto-measuring the specs](#auto-measuring-the-specs) |
| POST | `/devices/measure` | measure the whole fleet, bandwidth contention included |
| POST | `/control/connect` | open sessions; accepts the UI's per-device form |
| POST | `/control/disconnect` | close sessions (omit `device_ids` for all) |
| POST | `/control/exec` | fan a command across devices; long ones keep running |
| POST | `/control/exec/stop` | Ctrl-C whatever is still running |
| GET | `/control/jobs` | commands still running |
| POST | `/control/scp` | multipart push of one file to many devices |
| GET | `/control/status` | current per-device SSH status |
| GET | `/control/allowed-commands` | the allow-list the guard enforces |
| GET | `/control/audit` | destructive commands run or refused |
| POST | `/reports/analyze` | pull a result directory, chart it, save it as a report |
| GET | `/reports`, `/reports/{id}` | saved reports, newest first |
| PUT | `/reports/{id}/notes` | per-chart notes + the overall review |
| GET | `/reports/{id}/imgs/{name}` | one chart PNG |
| DELETE | `/reports/{id}` | delete a saved report |
| GET | `/server/config` | server host + SSH/AMQP users, **without** either password |
| POST | `/server/config` | upsert host, SSH login, AMQP login, jump-host flag |
| POST | `/server/test` | SSH login + AMQP handshake + control-API probe, reported per leg |
| GET/POST | `/clusters`, `PATCH /clusters/{id}` | per-cluster model / batch / num_bit / cut |
| GET/PATCH | `/config` | global clustering + cut-selection mode |
| GET/POST/DELETE | `/models` | model registry, incl. uploaded layer tables |
| GET/POST/DELETE | `/keys` | SSH key registry (write-only key material) |
| POST | `/run/deploy` | push shards + agent code, optionally install deps |
| POST | `/run/start` | begin split inference |
| POST | `/run/stop` | stop agents, drain queues |
| GET | `/run/active`, `/run/history` | run state |
| GET | `/metrics/latest` | snapshot, one §6 payload per cluster |
| GET | `/export` | inventory back in the UI's export shape (no credentials) |
| WS | `/ws/stream` | `ssh_status`, `exec_line`, `metrics`, `event` frames |
| GET | `/`, `/runtime-config.js`, `/{asset}` | the website (no token; see above) |

### WebSocket envelopes

```json
{"type": "snapshot",      "ssh_status": {...}, "server_status": "on", "metrics": [...], "recent": [...]}
{"type": "ssh_status",    "device_id": "d1", "status": "on"}
{"type": "server_status", "status": "on", "rabbitmq_version": "4.1.0", "api": "up"}
{"type": "exec_line",     "device_id": "d1", "text": "31% util", "stream": "stdout"}
{"type": "metrics",       "payload": { ... }}
{"type": "event",         "name": "run_started", "cluster": 1}
{"type": "ping"}
```

A `snapshot` is always sent first so a reconnecting client repaints immediately.
Every envelope carries an ISO-8601 `ts`.

### Metrics payload

Matches guide §6, so the UI renders it unchanged:

```json
{"cluster": 1, "cut": 6, "edge_ms": 12.4, "transfer_ms": 30.1, "cloud_ms": 4.2,
 "e2e_ms": 46.7, "msg_mb": 0.19, "fps": 21.3,
 "edge_util": 0.41, "transfer_util": 1.0, "cloud_util": 0.14,
 "queue_depth": 3, "devices": [{"id": "d1", "util": 0.44, "role": "head"}],
 "source": "live"}
```

`source` is `"live"` for a running cluster and `"sim"` when the value came from
the built-in simulator. Idle clusters report `{"cluster": n, "idle": true,
"reason": "..."}`, mirroring `simCluster()` returning `null`.

---

## Running split inference for real

### 1. Build the shards

`head.pt` / `tail.pt` must be **TorchScript** modules so the devices need no
model class definition:

```bash
python tools/split_model.py --weights yolo11n.pt --list-cuts   # which cuts are valid
python tools/split_model.py --weights yolo11n.pt --cut 6       # writes shards/
```

YOLO is not a plain `Sequential` — neck/head layers consume activations from
several earlier layers. A cut is only usable if no layer after it reads
something produced before it; `--list-cuts` reports which indices qualify and
splitting at a bad one is refused rather than producing a shard that fails at
inference time.

### 1b. Point the backend at your server

The Control tab's top card describes **two machines**, because they are usually
not the same one:

* **the control server** (`ip`, `ssh_*`) — the machine you SSH into, and
  optionally the jump host your devices sit behind;
* **the broker** (`amqp_host`, `port`, `user`) — where RabbitMQ runs. Leave
  `amqp_host` blank and it inherits `BROKER_URL`, i.e. the broker this backend
  is already connected to.

The SSH and AMQP logins are stored separately because they are almost never the
same account (`dai` vs `guest`). Both passwords are accepted on write only,
encrypted into the secret store, and never returned.

```bash
curl -X POST localhost:8000/server/config -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ip":"100.68.127.89",
       "ssh_port":22,"ssh_user":"dai","ssh_password":"…","jump_enabled":true,
       "amqp_host":"192.168.1.20","port":5672,"user":"guest","password":"guest"}'

curl -X POST localhost:8000/server/test -H "Authorization: Bearer $API_TOKEN"
# {"ok":true,"ssh":"ok","ssh_banner":"Linux srv 6.8.0 …",
#  "rabbitmq_version":"4.1.0","api":"up",...}
```

`/server/test` checks all three legs in parallel and reports each separately —
"it doesn't work" is useless when three different things can be wrong:

- **ssh** — logs in and runs `uname -a`, so a pass proves the credentials work
  *and* that commands execute, not merely that port 22 accepted a TCP connection.
  Reports `skipped` when no SSH user is on file, and does not drag `ok` down.
- **amqp** — a real handshake against `amqp_host`, reading the broker's
  advertised version.
- **api** — the control API's `/health`, probed on loopback: the control API is
  *this* process, so pointing the probe at the SSH gateway would report `down`
  for a perfectly healthy deployment.

It broadcasts `{"type":"server_status", …}` so the UI dot updates without polling.

### 1c. Commanding the server, and reaching devices through it

The server is addressable as a target under the reserved id **`__server__`**, so
every Control-tab action works on it:

```bash
curl -X POST localhost:8000/control/exec -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"device_ids":["__server__"],"command":"nvidia-smi"}'
```

In the UI it appears as its own **Control server** group at the top of Targets —
tick it, type a command, Run. The same allow-list, audit log, and `exec_line`
streaming apply as for any device.

The group's own **select** button also loads the command saved as `run server`,
the way each stage's **select all** loads `run stage <n>` (matched by preset
label, so renaming one in Control ▸ edit moves the wiring with it, and a list
without those labels simply leaves the command box alone).

It is deliberately **not** a row in the `device` table. It has no GFLOPS and
belongs to no cluster; a phantom device would be picked up by `build_clusters()`
and skew every metric the simulator produces. It is synthesized from
`ServerConfig` at resolution time instead (`app/ssh/gateway.py`).

#### Long-running commands

`python3 src/Server.py` runs for the length of an experiment, which no HTTP
request can be held open for. So `/control/exec` does not try:

- Commands run **under a pty**. Without one, python sees it is not writing to a
  terminal and block-buffers stdout, which for a long run means the output
  arrives in slabs — usually all of it at the very end. With one it is
  line-buffered and reaches the console as it is printed.
- The call answers as soon as the command finishes **or** after
  `EXEC_DETACH_AFTER` seconds (default 5), whichever comes first. `ls` finishes
  inside that window and is reported inline exactly as before; `Server.py` does
  not, and is listed in the response's `running` array while its output keeps
  streaming as `exec_line` frames.
- **There is no deadline on the run itself.** `SSH_COMMAND_TIMEOUT` still caps
  the internal one-shot probes, but capping an operator's command at 120 s meant
  killing a measurement run mid-flight, before `Server.py` could write the
  result logs that were the point of running it. Pass `timeout` explicitly if
  you want one.
- Stop it with `POST /control/exec/stop` — in the UI, type `^C` in the command
  box and press Run. It escalates SIGINT → SIGTERM → channel close, so a run
  that flushes its results on the way out gets the chance to. The SIGINT is
  delivered by writing `\x03` into the pty rather than as an SSH `signal`
  request, which OpenSSH's sshd accepts and ignores.
- `GET /control/jobs` lists whatever is still running.

A command started this way is tied to its SSH channel, so it dies if the session
is closed. To outlive that, launch it detached the way the orchestrator launches
agents (`nohup … >> log 2>&1 &`), which needs `ALLOW_UNSAFE_COMMANDS=true`
because it uses redirects and `&`.

With **`jump_enabled`** (the card's *Reach devices through this server*
checkbox), every device connection is opened over a channel on the server's own
connection — asyncssh's `tunnel=`, which is OpenSSH's `ProxyJump`. That is the
usual lab shape: one routable machine, and the edge/cloud nodes on a private
network behind it. A device's `host` is then resolved *by the server*, so
`10.0.1.x` addresses the control plane cannot route to are exactly the point.
The server never tunnels through itself, and if the jump host is unreachable the
device's error names both ends rather than silently retrying direct.

**`$BROKER_IP` follows the broker, not the gateway.** The `iperf3` and `ping`
presets run *on the devices* to measure their link to RabbitMQ, so the token
substitutes `amqp_host`. Substitution is refused outright when that resolves to
a loopback address: `iperf3 -c localhost` on a Jetson measures the Jetson's own
loopback and reports tens of Gbit/s of fiction, which `/devices/{id}/probe`
would then write into that device's bandwidth spec. A hard failure beats a
plausible wrong number.

This host is also what `$BROKER_IP` resolves to in the `iperf3` and `ping`
presets, so set it before using those.

### 2. Register devices and connect

```bash
# store a key once -- the private key is never returned by any endpoint
curl -X POST localhost:8000/keys -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"lab\",\"private_key\":$(jq -Rs . < ~/.ssh/id_ed25519)}"

curl -X PATCH localhost:8000/devices/dA -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"host":"10.0.1.10","username":"ubuntu","auth_method":"key","key_ref":"lab"}'

curl -X POST localhost:8000/control/connect -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"device_ids":["dA","dG1"]}'
```

### 3. Fill in real specs

Nobody should be typing GFLOPS into a form. In the UI there is a **⟳ measure**
button in each stage's header bar, next to the kind selector: it SSHes into
every device in that stage and replaces their GFLOPS / MB/s / LAT MS with what
it measured, streaming progress into the Control console. A field the hardware
could not answer for is left alone rather than zeroed.

Or from the API — one call fills the whole fleet:

```bash
curl -X POST localhost:8000/devices/measure -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

See [Auto-measuring the specs](#auto-measuring-the-specs) for what it measures
and why the bandwidth phase behaves differently from the other two. The older
`/devices/{id}/probe` still exists and is unchanged:

```bash
curl -X POST localhost:8000/devices/dA/probe -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"benchmark":true,"iperf_server":"10.0.0.5","latency_target":"10.0.0.5"}'
```

Its `gflops` is a torch matmul when torch is present, and a vendor peak-FP32
table otherwise (flagged in `warnings`). Bandwidth is measured only when
`iperf_server` is given; otherwise it reports the NIC's link speed, also
flagged.

### 4. Deploy and run

```bash
curl -X POST localhost:8000/run/deploy -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"cluster":1,"install_deps":true}'

curl -X POST localhost:8000/run/start -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"cluster":1}'
```

Deploy pushes the agents, `codec.py`, `bootstrap.sh`, and the shards to
`REMOTE_ROOT`, verifying each shard's sha256 after transfer. Start declares the
queues, launches the cloud (consumer) agents before the edge (producer) agents so
nothing piles up unread, confirms each PID survived import, and rolls the whole
cluster back if any agent fails to start.

The cut layer for a live run comes from the same simulator the UI uses, so live
and simulated results are directly comparable. `/run/start` returns the
simulator's forecast under `predicted` alongside the live run's id.

### 5. Chart what the run produced

The Control tab's **Visual** card (under Files) turns a finished run's result
directory into charts. Point it at the directory the run wrote on the device,
give the run a case-test name, and press **Analyze**:

```bash
curl -X POST localhost:8000/reports/analyze -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"d3","path":"ntuanh/TimeProfiler/run7","case_name":"cut6-8bit"}'
```

The backend walks the directory over SFTP (text files only, bounded to 400
files / 256 MB / 8 levels), reads the **plain-text logs** for `name value unit`
measurements, and picks a chart form per metric from the data's job — readings
over a run become a trend, one reading per file becomes a comparison, a single
number becomes a stat tile rather than a one-bar bar chart. CSV and JSON in the
same directory are read too.

Charts are rendered server-side with matplotlib to **`guides/visual_guide.md`**:
slot-ordered categorical colors, solid hairline grids, no dual axes (metrics of
very different scale are split into panels), boxplot annotations above the
whisker cap, and delta charts coloured by *verdict* rather than by sign. The
guide's §3 palette validator is ported to `app/reports/palette.py` — `node` is
not installed here — and `tests/test_reports.py` pins it to the guide's own
sanity numbers (worst CVD ΔE 9.1, worst normal ΔE 19.6).

Each chart gets a note box for a short review; **Save report** writes the notes
and collapses the card. A report is a folder, not a database row:

```
reports/2807-1432_cut6-8bit/       # day-month-hour-minute, then the case name
  manifest.json                    # charts, tiles, files read, notes, review
  imgs/01_stage_breakdown.png
  imgs/02_comparison.png
```

Copying that folder is the whole export story. `REPORTS_DIR` moves the tree.

A **History** bar at the top of the card browses past days: the first row is a
day picker (`Today · 2`, `Yesterday · 2`, `24 Jul · 1`), the second lists that
day's runs by time (`14:32 cut6-8bit ✎`, where the pip marks a run that was
actually reviewed). Clicking one re-opens its charts and notes. `GET /reports`
returns the day buckets alongside the rows, and always computes them over the
whole listing — so `?day=2026-07-27` narrows the reports without hiding the
fact that other days have results.

---

## How the pieces fit

```
app/
  main.py            FastAPI app, CORS, routers, /ws/stream
  config.py          pydantic-settings
  db.py              async SQLModel engine/session
  models.py          Device, Cluster, Run, KeyRef, ModelDef, GlobalConfig
  schemas.py         request/response models (UI aliases: bw, lat, cluster)
  auth.py            bearer-token stub (REST + WS)
  ssh/
    pool.py          per-device connection reuse, status events, ProxyJump
    commands.py      run_command, fan_out, scp_put, allow-list guard,
                     pty streaming + the running-job registry
    gateway.py       the control server as a target `__server__` + jump host
    secrets_store.py keys + passwords on disk, never served
  inference/
    simulation.py    port of the UI's math (see "Parity" below)
    broker.py        aio-pika connect/declare/publish/consume/depth
    orchestrator.py  deploy, start/stop, rolling-window metric aggregation
  reports/
    parse.py         result logs -> metric series (plain text, CSV, JSON)
    charts.py        matplotlib rendering, per guides/visual_guide.md
    palette.py       the guide's palette + its CVD/contrast validator
    store.py         a saved report as a folder: manifest.json + imgs/
  routers/           devices, control, clusters, run, metrics, reports, web
    web.py           serves web/ at `/`; token injection for local browsers
  services/
    metrics_bus.py   in-proc pub/sub -> WebSocket
    topology.py      DB inventory -> cluster shape (port of buildClusters)
agent/               runs ON the devices: edge_agent, cloud_agent, codec, bootstrap.sh
tools/
  split_model.py     head.pt / tail.pt exporter
  build_web.py       bundled UI -> web/  (unpack + patch)
ui/
  backend-client.js  transport layer: REST + WS + payload shaping
  live-patch.js      the Component.prototype overrides the build appends
  WIRING.md          wiring the client into a page of your own
web/                 generated -- the served site (gitignored)
tests/               hermetic tests + 7 broker-integration tests
```

### Metric collection

Cloud agents publish one JSON report per frame to `metrics_queue`. The
orchestrator keeps a rolling window (`METRICS_WINDOW` frames) per cluster and
broadcasts an aggregated §6 payload at `METRICS_BROADCAST_HZ`:

- `edge_ms` / `transfer_ms` / `cloud_ms` — window means
- `fps` — frames completed per second from arrival timestamps, not from `e2e_ms`
- `*_util` — each stage's mean over the pipeline bottleneck (`max` of the three)
- `queue_depth` — immediate passive declare on the intermediate queue
- `devices[].util` — that device's own stage time over the bottleneck
- `stale: true` once no report has arrived for 5s

---

## Auto-measuring the specs

`app/services/measure.py`, behind `POST /devices/measure` and
`POST /devices/{id}/measure`. It exists so that `gflops` / `bw` / `lat` on a
device card are read off the hardware rather than typed by whoever set it up —
those three numbers feed the simulator directly, so a guess in the form becomes
a wrong cut layer.

| Field | Method | Fallbacks |
|---|---|---|
| `gflops` | 3×3 convolution benchmark, auto-tuned to a fixed time budget | matmul → vendor peak-FP32 table (both flagged) |
| `bandwidth_mb_s` | timed pull of an incompressible blob over the open SSH connection | `iperf3` when `iperf_server` is given → NIC link speed (flagged) |
| `latency_ms` | TCP handshake to the broker's AMQP port, minimum of 7 | ICMP → SSH command round trip (flagged) |

Every number carries its provenance in `sources` (`conv-fp32`, `sftp-blob`,
`tcp-connect`, …) and unmeasurable fields are left at whatever they were, so a
device without torch keeps the GFLOPS an operator typed rather than having it
zeroed.

Convolution rather than a matmul because the pipeline runs a CNN, and a CNN
reaches a very different fraction of peak than a big GEMM does — on most GPUs
the matmul figure is several times higher. The simulator turns `gflops` straight
into milliseconds (`cum[cut] / gflops`), so the optimistic number becomes an
optimistic schedule. The matmul is still measured and kept in `info` for
comparison.

TCP rather than ICMP because a completed handshake is one round trip over the
transport the pipeline actually uses, and it keeps working on the many networks
that drop ping by default — every cloud provider's stock security group, for one.

### Why bandwidth is measured differently

Compute is local to a device and latency costs a handful of packets, so both are
measured on the whole fleet at once. Bandwidth cannot be: the devices share an
uplink and a broker, so twenty machines measuring together each report *their
share of one link* rather than their own capacity. The fleet pass therefore runs
the transfer test **strictly one device at a time**, and a single-device
`/devices/{id}/measure` takes the same lock, so firing one off during a fleet run
queues behind it instead of spoiling both numbers.

That yields the **solo** figure. It is not what a device gets during a run, when
every stage publishes at once — so `contention: true` (the default) adds a second
pass with all devices transferring simultaneously and records the **shared**
figure beside it:

```jsonc
{
  "device_id": "dA",
  "bandwidth_solo_mb_s": 112.4,     // the link to itself
  "bandwidth_shared_mb_s": 31.8,    // the link during a run
  "contention_ratio": 0.283,        // shares an uplink with three others
  "sources": {"bandwidth": "sftp-blob"}
}
```

The ratio is per device, so machines on independent links (≈1.0) separate
themselves from machines fighting over one uplink (well under 1.0) without this
service needing to be told the network topology. Anything under 0.8 also comes
back as a warning.

`bandwidth_basis` picks which figure is written to the spec field:

- `shared` (default) — what a run actually gets, so the simulated transfer times
  describe the pipeline you are going to run. Falls back to solo when no
  contention pass ran, which is also correct there: with nothing to contend
  with, solo *is* the operating figure.
- `solo` — the link's own capacity, for answering "did this Jetson negotiate
  gigabit or 100 Mbit?", where another device's traffic is noise.

Both figures and the basis used are always stored in `probe_info`, whichever one
was applied.

### From the UI

The **⟳ measure** button in each stage's header bar runs the whole thing for
that stage's devices and writes the results onto the cards
(`siMeasureStage` → `siApplyMeasured` in [`ui/live-patch.js`](ui/live-patch.js)).

A stage rather than a device is the unit because bandwidth is: the solo and
contended figures only mean something relative to each other for a set of
machines that actually share a link, and a stage is the closest thing the UI has
to that set — the edges sit behind one uplink, the cloud nodes behind another.
The console gets a line per device with its `sources`, every warning the backend
raised, and the group's contention summary; a field that could not be measured
keeps whatever was in the box. It works in Simulate mode too — which math draws
the charts has nothing to do with whether the hardware can be timed, and
Simulate is what computes *from* these numbers.

### Known limits

- The transfer test runs over SSH, so it is net of encryption overhead on a
  single stream — a slight under-estimate of the raw link. `iperf3` avoids that
  when an iperf server is reachable; `nic_link_mbit` in `info` is the sanity
  check either way.
- The contended pass measures the whole fleet at once, which is the worst case.
  A run only loads the devices in the clusters it touches.
- The convolution benchmark is a proxy, not the model. Timing the deployed shard
  would be more faithful, but `head.pt` exists only on the edges and its input
  shape is known while `tail.pt` takes the head's feature map, whose shape cannot
  be synthesized here — measuring the two sides by different methods would bias
  exactly the comparison the split decision rests on. The real ground truth is a
  completed run: per-stage times from `metrics_queue` can be solved back into an
  effective GFLOPS per device, which is the intended next step.

---

## Parity with the UI simulator

`app/inference/simulation.py` is a line-for-line port of the UI's math, verified
against the original JS rather than by inspection: the formulas from
`split-inference-pipeline.html` were run in an independent JavaScript engine and
the output stored as `tests/golden_ui_math.tsv`. `tests/test_simulation.py`
asserts the Python reproduces all 18 cluster scenarios (power/latency balancing,
4/8/16/32-bit, manual and per-cluster cut overrides) across all 14 metric fields,
plus all 36 layer definitions of the three built-in models.

Two JS behaviours needed explicit handling: `Math.round` and `toFixed` round
halves away from zero, whereas Python's `round` is banker's rounding —
`sim._round` / `sim._fixed` restore the JS semantics so scaled model layers match
byte-for-byte.

If the UI's formulas change, re-run that harness and refresh the golden file.

---

## Security notes

**Command allow-list.** `POST /control/exec` accepts only allow-listed prefixes
(all 14 UI presets: `nvidia-smi`, `uptime`, `nproc`, `df`, `free`, `iperf3`,
`ping`, `systemctl … inference-agent`, `journalctl -u inference-agent`,
`sudo reboot`, `python -c`, plus read-only probes, `python3 -m agent…`, and
`python3 <script>.py`) and rejects **unquoted** shell metacharacters
(`; & | < > \` \\ $( ${`).
`ALLOW_UNSAFE_COMMANDS=true` disables the metacharacter and allow-list checks —
only sensible if you own every machine in the inventory. It does *not* disable
the destructive-command confirmation, which guards against accidents rather than
against a hostile operator.

The metacharacter scan is quote-aware, because the "python ver" preset is
`python -c "import torch;print(…)"` — that `;` is inside quotes and is a single
command. Shell rules are applied: single quotes make everything literal, while
inside double quotes `$(`, `${`, and backticks still expand and are still
rejected. Unbalanced quotes are refused.

**`python -c` is deliberately narrow.** Allowing it in general is arbitrary code
execution (`python -c "import os;os.system(...)"`) and would make the whole
allow-list meaningless. Only read-only introspection passes: the script must
import from a small module set (torch, numpy, pika, platform) and must not
mention `os`, `sys`, `subprocess`, `open`, `eval`, `exec`, `__import__`, and
similar. The documented preset works; a shell escape does not.

**`python3 <script>.py` is allowed** — `python3 src/Server.py --config x.yaml`
and the like. Starting the project's own scripts is what this console is for on
the control machine, and doing it here is the same act as doing it over SSH by
hand, which is the alternative. The rule is narrower than a `python3` prefix:
the first non-flag argument must be a `.py` file, so it re-admits neither the
bare REPL (no terminal — it would hang) nor `-c` / `-m`, which keep their own
much stricter rules. Only argument-less interpreter flags (`-u`, `-B`, …) may
precede the script.

**Destructive commands need `confirm=true`.** `sudo reboot`, `shutdown`, and
`systemctl stop|restart inference-agent` return **409** unless the request sets
`confirm: true`. Every attempt — granted or refused — is written to the
`AuditLog` table and readable at `GET /control/audit`.

**`$BROKER_IP` substitution.** The `iperf3` and `ping` presets use the
`$BROKER_IP` token, replaced with the host from `POST /server/config`.
Substitution happens **before** validation, so the allow-list inspects the exact
string that will reach the shell — a host value containing `;` cannot smuggle a
second command past the guard. If no host is configured the command is rejected
rather than letting the shell expand the token to an empty string.

The guard runs at the **router boundary**, not inside `run_command`: commands the
orchestrator builds itself legitimately need redirects and `&` for
`nohup … > log 2>&1 &`. Validating operator input where it enters the system
keeps that distinction explicit instead of relying on a flag every internal
caller could forget.

**Secrets.** Private keys live in `SECRETS_DIR/<key_ref>.pem`; passwords and key
passphrases in `SECRETS_DIR/credentials.json`, **encrypted with Fernet**
(AES-128-CBC + HMAC) and chmod 0600 where the OS supports it. The master key
comes from `SECRET_ENCRYPTION_KEY` or is generated once into
`SECRETS_DIR/.master.key`. Nothing secret is stored in SQLite and no endpoint
returns it — `GET /devices` exposes only `has_password`, and `GET /server/config`
only `has_credentials`. Values written before encryption was added remain
readable and are re-encrypted on next write. `key_ref` is validated against a
strict charset and resolved inside `SECRETS_DIR` so it cannot traverse out.
`GET /export` omits hosts, usernames, key refs, and passwords so it stays
shareable.

> Back up `.master.key` alongside `credentials.json`. Losing it means every
> stored password has to be re-entered; a mismatched key is reported in the log
> rather than silently returning garbage to the SSH client.

**Host keys.** `known_hosts=None`: lab devices' host keys churn and the UI has no
place to accept a fingerprint. This trades MITM protection for usability and is
appropriate on a trusted network — not over the public internet. This applies to
the jump host too: tunnelling through it does not add host-key verification at
either hop.

**The control server's SSH password** is filed in the secret store under the
reserved target id `__server__`, the same path as a device password, so the pool
resolves it with no special case. `ServerConfig.SSH_SECRET_REF` must equal
`gateway.SERVER_DEVICE_ID` for that to hold, and `gateway` asserts it at import
time — the failure it prevents is silent, since the password would be stored
successfully and simply never found again.

**SCP paths.** `remote_path` must be absolute and free of `..`; uploads spool to
disk (shards are hundreds of MB) and are capped at 2 GiB.

**CORS.** The served UI is same-origin, so CORS does not apply to it at all.
`CORS_ORIGINS` matters only when the page is hosted elsewhere; it defaults to
`*` with `allow_credentials=False`, which is safe for bearer tokens. Opening the
bundled HTML from disk sends `Origin: null`, so add `null` explicitly if you
narrow the list.

**The website is public, its API is not.** `/`, its assets, and
`runtime-config.js` are served without a token — a browser cannot attach a
header to a top-level navigation, and the page is a static artifact either way.
Everything it then *calls* requires the token. `runtime-config.js` hands that
token over only when the request came from loopback, decided from the socket
address rather than a forwardable header, so a remote visitor cannot claim to
be local. `WEB_AUTOFILL_TOKEN=false` turns the shortcut off entirely and the
operator pastes the token into the header chip.

The asset route resolves every path inside `web/` and rejects anything that
escapes, so `../.env` and friends 404 rather than serving the secrets that sit
one directory up.

---

## Operational caveats

- **`transfer_ms` needs synced clocks.** It is measured as
  `t_consume - t_publish` across two machines. Without NTP it is meaningless;
  negative deltas are clamped to 0 and flagged as `clock_skew` on the report. For
  a single-machine sanity check, run both agents on the same host.
- **The broker is optional at boot.** Simulate mode and the entire Control tab
  work without RabbitMQ; `/run/start` reconnects on demand and answers 503 if it
  cannot.
- **Jetson/Tegra needs NVIDIA's torch wheel.** `bootstrap.sh` detects Tegra and
  stops with instructions rather than installing a PyPI torch with no CUDA
  support for that platform.
- **`num_bit` above 16 is rejected by the codec.** The simulator will happily
  model 32-bit messages, but the wire format quantizes to at most 16 bits.
- **`queue_depth` deliberately avoids the management API.** RabbitMQ's
  `messages_ready` only refreshes on its statistics interval (~5s), which would
  make the gauge lag; a passive declare is immediate. The management API is used
  only for consumer count and ack rate.
- **Rebuild the site after re-exporting the UI.** `tools/build_web.py --check`
  compares the bundle's hash against the last build and exits non-zero when it
  has moved — worth wiring into your deploy step.
- **One harmless 404 on page load.** The DC runtime paints the raw template once
  before the first render, so `<img src="{{ demoSrc }}">` briefly requests
  `/%7B%7B%20demoSrc%20%7D%7D`. It is the runtime's placeholder pass, present in
  the original bundle too, and the next frame replaces it.

---

## Tests

```bash
pip install pytest httpx numpy quickjs
pytest -q                                  # hermetic; no broker, no network
pytest tests/test_broker_integration.py    # +7, needs RabbitMQ (else skipped)
```

`quickjs` is optional: it runs `ui/live-patch.js` in a real JavaScript engine so
the browser-only handlers are covered like everything else. Without it,
`tests/test_ui_measure.py` skips and the rest of the suite is unaffected.

Covered: seeding the UI export, simulator parity, cut-selection modes and
overrides, the message-size guard, device CRUD, the command allow-list (including
injection attempts), SCP path validation, credential non-disclosure, WebSocket
auth and framing, the codec's round trip at 2/4/6/8/12/16 bits, spec measurement
(that solo bandwidth readings never overlap while the contention pass does, and
that the on-device benchmark's arithmetic is right — checked against a stub torch
whose ops take a known time, since no CI box has a GPU), live metric
aggregation through a real broker, and the website: that the build applies both
patches, rewrites every asset, refuses to build when a patch anchor moves, and
that the asset route neither shadows the API nor serves anything outside `web/`.

The suite is order-independent: each test gets a fresh database.

---

## Configuration

See `.env.example` for the annotated list. The ones that matter most:

| Variable | Why it matters |
|---|---|
| `API_TOKEN` | the only credential; change it |
| `WEB_AUTOFILL_TOKEN` | hand the token to local browsers so `/` is live at once |
| `CORS_ORIGINS` | only matters for a UI served from somewhere else |
| `BROKER_URL` | RabbitMQ as **this service** reaches it |
| `DEVICE_BROKER_URL` | RabbitMQ as **the devices** reach it |
| `REMOTE_ROOT` | install prefix on each device |
| `SHARDS_DIR` | where `/run/deploy` reads `head.pt` / `tail.pt` |
| `REPORTS_DIR` | where saved chart reports are written (one folder each) |
| `MAX_MESSAGE_MB` | mirrors the UI's `maxMessageMb` prop (15) |
| `ALLOW_UNSAFE_COMMANDS` | disables the exec guard |
| `METRICS_WINDOW`, `METRICS_BROADCAST_HZ` | smoothing and push rate |
