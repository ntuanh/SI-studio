# Backend Implementation Guide — Split Inference Control Plane

> For Claude Code. Build the backend that powers the **Split Inference Simulation Platform** UI. The Control tab configures a **broker/backend server** (IP/port/user/password, Test connection) plus per-device SSH targets, and fans commands out across many machines.

---

## 1. Goal & scope

Implement a Python **FastAPI** service that:

1. Registers edge/cloud machines and their specs (mirrors the UI's device model).
2. Opens/closes **SSH** sessions and runs **fan-out commands** across many devices (Control tab).
3. Pushes files over **SCP/SFTP** (model shards: `head.pt`, `tail.pt`).
4. Deploys a split model per the chosen **cut layer** and runs **split inference** over **RabbitMQ (AMQP)** queues — edge runs `layers[:cut]`, cloud runs `layers[cut:]`.
5. Streams **live metrics** (utilization, queue depth, fps, e2e latency) back to the UI over WebSocket.

**Non-goals:** training, model conversion, auth UI. Provide a single API-token auth stub only.

---

## 2. Tech stack (use exactly this unless noted)

| Concern | Choice |
|---|---|
| API | FastAPI + Uvicorn |
| SSH/SCP | `asyncssh` (async, concurrent fan-out) |
| Message broker | RabbitMQ via `aio-pika` |
| Data models | Pydantic v2 |
| Persistence | SQLite via SQLModel (devices, clusters, runs) |
| Secrets | SSH keys on disk under `secrets/`, referenced by `key_ref` id — **never** returned over the API |
| Realtime | FastAPI WebSocket (`/ws/stream`) |
| Config | `pydantic-settings` + `.env` |

Python 3.11+. Use `async`/`await` throughout.

---

## 3. Project layout

```
backend/
  app/
    main.py            # FastAPI app, CORS, router mounting, WS endpoint
    config.py          # Settings (env)
    db.py              # SQLModel engine/session
    models.py          # SQLModel tables: Device, Cluster, Run, KeyRef
    schemas.py         # Pydantic request/response
    ssh/
      pool.py          # AsyncSSH connection pool (per-device, reused)
      commands.py      # run_command, fan_out, scp_put
    inference/
      broker.py        # aio-pika connection, queue declare/publish/consume
      orchestrator.py  # deploy shards, start/stop split run, collect metrics
    routers/
      devices.py
      control.py       # ssh/scp/fan-out endpoints
      run.py           # deploy + run split inference
      metrics.py
    services/
      metrics_bus.py   # in-proc pub/sub -> WebSocket broadcast
  agent/
    edge_agent.py      # runs ON each device: consumes frames, runs head, publishes feature map
    cloud_agent.py     # consumes feature map, runs tail + NMS, publishes detections/metrics
  secrets/             # gitignored; SSH private keys
  requirements.txt
  .env.example
  README.md
```

---

## 4. Data model (mirror the UI)

The UI device carries: `name`, `gflops`, `bw` (MB/s), `lat` (ms), `cluster` (id), stage `kind` (Edge/Fog/Cloud/Custom). Extend with connection fields.

```python
class Device(SQLModel, table=True):
    id: str = Field(primary_key=True)          # matches UI device id
    name: str
    kind: str                                  # Edge | Fog | Cloud | Custom
    cluster_id: int
    host: str                                  # ip or dns
    port: int = 22
    username: str
    key_ref: str                               # -> secrets/<key_ref>.pem
    gflops: float
    bandwidth_mb_s: float
    latency_ms: float
    role: str = "auto"                         # head | tail | auto (derived from kind)

class Cluster(SQLModel, table=True):
    id: int = Field(primary_key=True)
    queue_name: str                            # intermediate_queue_<id>
    model_name: str
    batch_size: int = 32
    num_bit: int = 8                           # compression
    cut_layer: int | None = None               # manual override, else auto
```

---

## 5. SSH layer (`app/ssh/`)

### pool.py
- `class SSHPool` holding `dict[device_id, asyncssh.SSHClientConnection]`.
- `async def get(device) -> conn`: reuse if open, else `asyncssh.connect(host, port, username, password=<from secret store>` **or** `client_keys=[key_path(key_ref)]` depending on `auth_method`, `known_hosts=None)`.
- `async def connect_all(devices)` / `disconnect_all()`.
- Emit status transitions (`off|connecting|on|error`) to `metrics_bus` so the UI dots update.

### commands.py
```python
async def run_command(conn, cmd: str) -> CmdResult:
    r = await conn.run(cmd, check=False)
    return CmdResult(stdout=r.stdout, stderr=r.stderr, exit=r.exit_status)

async def fan_out(pool, device_ids, cmd) -> list[CmdResult]:
    # asyncio.gather across devices, bounded by a Semaphore(8)

async def scp_put(conn, local_path, remote_path):
    await asyncssh.scp(local_path, (conn, remote_path))
```
**Security:** validate `cmd` against an allow-list prefix set (`nvidia-smi, uptime, nproc, df, free, systemctl status|restart inference-agent, python -m agent...`). Reject arbitrary pipes/`;`/`&&` unless an `--unsafe` flag is set server-side.

---

## 6. Inference orchestration (`app/inference/`)

Split inference runtime (mirrors the sim's formulas — see §4 of `split_inference_rebuild_spec.md`):

1. **Deploy** (`orchestrator.deploy(cluster)`): scp `head.pt` to each edge device, `tail.pt` to each cloud device in the cluster; start `edge_agent.py`/`cloud_agent.py` via SSH (`nohup python -m agent... &`).
2. **Queues** (`broker.py`): declare `intermediate_queue_<cluster>` (durable), plus `fps_queue` and `metrics_queue`. Edge publishes compressed feature maps; cloud consumes.
3. **Run**: edge agent captures/loads frames → runs `layers[:cut]` → quantizes to `num_bit` → publishes to the cluster queue. Cloud agent consumes → runs `layers[cut:]` → NMS → publishes detections + timing to `metrics_queue`.
4. **Collect**: orchestrator consumes `metrics_queue`, computes per-device utilization, queue depth, fps, e2e latency, forwards to `metrics_bus`.

Keep the **same metric shape** the UI already renders:
```json
{"cluster": 1, "cut": 6, "edge_ms": 12.4, "transfer_ms": 30.1, "cloud_ms": 4.2,
 "e2e_ms": 46.7, "msg_mb": 0.19, "fps": 21.3,
 "edge_util": 0.41, "transfer_util": 1.0, "cloud_util": 0.14,
 "queue_depth": 3, "devices": [{"id":"d1","util":0.44}]}
```

---

## 7. REST + WS API (mirror UI actions)

| Method | Path | Purpose | UI trigger |
|---|---|---|---|
| GET | `/devices` | list | Stages/Control load |
| POST | `/devices` | register | Add device |
| PATCH | `/devices/{id}` | edit specs | Device edit |
| DELETE | `/devices/{id}` | remove | Remove device |
| POST | `/devices/{id}/probe` | SSH in, read real `nvidia-smi`/`nproc`/`iperf3` → fill gflops/bandwidth | (new) auto-fill |
| POST | `/control/connect` | `{device_ids, credentials?}` open sessions (password/key resolved server-side) | Connect all |
| POST | `/control/disconnect` | close sessions | Disconnect |
| POST | `/control/exec` | `{device_ids, command}` fan-out | Control ▶ Run |
| POST | `/control/scp` | multipart file + `{device_ids, remote_path}` | Control Send |
| POST | `/clusters` / `PATCH /clusters/{id}` | per-cluster model/batch/num_bit/cut | Clusters tab |
| POST | `/run/deploy` | push shards + start agents | (new) Deploy |
| POST | `/run/start` | begin split inference | Run simulation → live |
| POST | `/run/stop` | stop agents, drain queues | Stop flow |
| GET | `/metrics/latest` | snapshot | Simulation load |
| WS | `/ws/stream` | push status + metrics frames | live dots, timeline, console |

### Broker/server config (Control tab top card)
The UI's **Broker / backend server** card sends `{ip, port, user, password}` for the RabbitMQ + control-API host and calls **Test connection**. Add:
| POST | `/server/config` | store broker host/port/user + password (secret store, same rules as §4) | edit fields |
| POST | `/server/test` | verify AMQP reachable + control API up; return `{ok, rabbitmq_version, api}` | Test connection |
The command presets reference `$BROKER_IP` (e.g. `iperf3 -c $BROKER_IP`, `ping -c 3 $BROKER_IP`) — the backend must substitute the configured broker IP into fan-out commands before execution.

WebSocket message envelope:
```json
{"type": "ssh_status", "device_id": "d1", "status": "on"}
{"type": "exec_line",  "device_id": "d1", "text": "31% util", "stream": "stdout"}
{"type": "metrics",    "payload": { ...see §6 }}
```

---

## 8. Wiring the existing UI

The UI already has the right state shape; replace the simulated calls:

- **Control tab** (`sshRun`, `sshScp`, `sshConnectAll` in the DC logic): swap the `setTimeout` mocks for `fetch('/control/exec' | '/control/scp' | '/control/connect')`, and append `exec_line`/`ssh_status` WS frames to `state.ssh.out` / `state.ssh.status`. The per-device connection form (`state.ssh.conn[id]` = `{ip, port, user, password}`) maps directly to the device record — POST it on save/connect; never keep the password in exported JSON.
- **Simulation/Pipeline**: replace client-side `simCluster()` with `/metrics/latest` + `/ws/stream` `metrics` frames; keep the timeline/utilization/queue-depth rendering as-is.
- Add a **"Live" vs "Simulate" toggle** in the header: Live routes through the backend, Simulate keeps the current in-browser math.
- Backend base URL from an env/localStorage setting; send the API token as `Authorization: Bearer`.

Keep field names identical (`gflops`, `bandwidth_mb_s`→`bw`, `latency_ms`→`lat`, `cluster`) so no UI refactor is needed beyond the transport.

---

## 9. Agents (run on the machines)

- `edge_agent.py`: args `--broker-url --queue --model head.pt --cut N --num-bit B --batch S`. Loop: get frame → `head(frame)` → quantize → `channel.basic_publish(queue, payload)` → record `edge_ms`.
- `cloud_agent.py`: args `--broker-url --queue --model tail.pt`. Consume → dequantize → `tail(fmap)` → NMS → publish detections + `{cloud_ms, transfer_ms, e2e_ms}` to `metrics_queue`.
- Package deps assumed present on devices (torch, ultralytics, pika). Provide a `bootstrap.sh` the orchestrator can scp+run to install them.

---

## 10. Deliverables checklist

- [ ] FastAPI app boots (`uvicorn app.main:app`), CORS allows the UI origin.
- [ ] SQLite auto-creates tables; seed endpoint to import the UI's exported JSON.
- [ ] `SSHPool` with reuse + status events; `fan_out` bounded concurrency; allow-list guard.
- [ ] `/control/exec`, `/control/scp`, `/control/connect|disconnect` working against a test VM.
- [ ] `/devices/{id}/probe` fills real specs.
- [ ] RabbitMQ queues declared; edge/cloud agents publish/consume; orchestrator collects metrics.
- [ ] `/ws/stream` broadcasts `ssh_status`, `exec_line`, `metrics`.
- [ ] `.env.example`, `requirements.txt`, `README.md` with run instructions and a `docker-compose.yml` (RabbitMQ + backend).
- [ ] Metric payloads match §6 so the existing UI renders unchanged.

---

## 11. Reference

The simulation math, design tokens, and current device/cluster scenario are captured in **`split_inference_rebuild_spec.md`** (exportable from the UI's **MD** button). Use it as the source of truth for formulas (`edge_ms`, `transfer_ms`, `cloud_ms`, message-size guard, power-proportional vs latency-minimizing cut selection) so live results are comparable to the simulator.
