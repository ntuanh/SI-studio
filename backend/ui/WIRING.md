# Wiring the UI to this backend (guide §8)

> **You probably don't need this page.** The wiring is already applied:
> `python tools/build_web.py` unpacks the bundled UI into `backend/web/` with
> [`live-patch.js`](live-patch.js) attached, and the backend serves the result
> at `/`. See the *The website* section of the [README](../README.md).
>
> What follows is the manual recipe — the call-by-call mapping the patch
> implements. Read it to understand what the bridge does, to embed
> `backend-client.js` in a page of your own, or to re-do the wiring by hand
> against a differently built UI.

The UI in `split-inference-pipeline.html` is a **bundled** page: the real app
lives inside a `<script type="__bundler/template">` JSON string, with assets in
`__bundler/manifest`. Editing it in place means editing that template string and
re-bundling — so the transport layer ships as a standalone drop-in
(`backend-client.js`) rather than a patched HTML file. Nothing below changes the
UI's state shape or its rendering.

Load it before the DC logic:

```html
<script src="backend-client.js"></script>
```

Then configure once (persisted in `localStorage`):

```js
SplitInference.configure({ baseUrl: 'http://localhost:8000', token: 'dev-token-change-me', mode: 'live' });
```

> In `live-patch.js` these snippets appear as overrides on
> `Component.prototype`, keeping the original method reachable so Simulate mode
> still runs the untouched in-browser math.

---

## 1. Live vs Simulate toggle

`SplitInference.config.mode` is `'simulate'` (default) or `'live'`. Add a header
button that flips it and re-renders; `simulate` keeps the current in-browser
math untouched.

```js
toggleMode() {
  const next = SplitInference.isLive() ? 'simulate' : 'live';
  SplitInference.configure({ mode: next });
  this.setState({ liveMode: next === 'live' });
  if (next === 'live') this.startLive(); else this.stopLive();
}
```

---

## 2. Control tab

The three mocked methods map one-to-one. Current `state.ssh` shape is unchanged:
`selected`, `status`, `conn`, `out`, `busy`.

### `sshConnectAll(on)`

```js
sshConnectAll(on) {
  const devs = this.flatDevices();
  const ids = devs.map(d => d.id);
  if (!on) {
    SplitInference.api.disconnect(ids)
      .then(() => this.sshLog([{ text: '⇄ all sessions closed', color: '#94A3B8' }]));
    return;
  }
  this.sshSetStatus(ids, 'connecting');
  this.sshLog([{ text: '⇄ opening SSH sessions…', color: '#22D3EE' }]);
  // Passwords come from state.ssh.conn and are sent once, over HTTPS in prod.
  SplitInference.api.connect(ids, this.state.ssh.conn)
    .catch(e => this.sshLog([{ text: '✗ ' + e.message, color: '#F87171' }]));
  // Per-device results arrive as ssh_status / exec_line frames on the WS.
}
```

### `sshRun()`

Destructive presets (**reboot**, **stop agent**, **restart agent**) answer
**409** unless `confirm` is set — use that to drive a confirmation dialog rather
than pre-guessing which commands are dangerous:

```js
sshRun(confirm) {
  const sel = this.state.ssh.selected;
  const cmd = this.state.ssh.command.trim();
  if (!sel.length || !cmd) return;
  this.sshLog([{ text: '$ ' + cmd + '   → ' + sel.length + ' host(s)', color: '#e2e8f0' }]);
  this.sshPatch({ busy: true });
  SplitInference.api.exec(sel, cmd, confirm)
    .catch(e => {
      if (e.status === 409) {
        // Destructive: ask, then re-send with confirm=true.
        this.setState({ confirmPrompt: { command: cmd, message: e.message } });
      } else {
        this.sshLog([{ text: '✗ ' + e.message, color: '#F87171' }]);
      }
    })
    .then(() => this.sshPatch({ busy: false }));
  // stdout/stderr stream in as exec_line frames; no need to render the response.
}
```

Rejected commands come back as HTTP 400 with the allow-list in the message.
`GET /control/allowed-commands` returns `{prefixes, destructive, python_inline,
python_script, broker_ip_token, broker_ip, unsafe_enabled, detach_after_s}`, so
the UI can mark the destructive presets with a warning glyph and disable the
`$BROKER_IP` ones until a broker host is configured.

### Long-running commands and `^C`

`exec()` resolves as soon as the command finishes **or** after the server's
detach window, whichever is first — it is never held open for the length of a
run. Anything still going is listed in the response's `running` array and keeps
streaming as `exec_line` frames, so there is nothing extra to render:

```js
// `python3 src/Server.py` answers in milliseconds and keeps running.
SI.api.exec(sel, cmd, false, cwd).then(r => {
  if (r.running.length) { /* the console already said so */ }
});
```

Stop it with `SI.api.stopExec(deviceIds)`. `live-patch.js` routes `^C` typed in
the command box there (see `INTERRUPT_RE`), because a run outlives the request
that started it: by the time you want to stop it there is no pending call left
to cancel and no shell session to interrupt.

`$BROKER_IP` in the **iperf3** and **ping broker** presets is substituted
server-side from `/server/config` — send the preset text verbatim. With no host
configured the call returns 400, so gate those buttons on
`serverConfig.host`.

### `sshScp()`

The UI currently only tracks a filename (`state.ssh.scpLocal`). Keep the `File`
object from the input's change event, since the backend needs the bytes:

```js
onScpFile(e) {
  const f = e.target.files && e.target.files[0];
  if (f) { this._scpFile = f; this.sshPatch({ scpLocal: f.name }); }
}

sshScp() {
  const sel = this.state.ssh.selected;
  if (!sel.length || !this._scpFile) return;
  const remote = this.state.ssh.scpRemote;
  this.sshLog([{ text: '⇪ scp ' + this._scpFile.name + ' → ' + sel.length + ' host(s):' + remote, color: '#e2e8f0' }]);
  this.sshPatch({ busy: true });
  SplitInference.api.scp(sel, this._scpFile, remote)
    .catch(e => this.sshLog([{ text: '✗ ' + e.message, color: '#F87171' }]))
    .then(() => this.sshPatch({ busy: false }));
}
```

### Saving the per-device connection form

`state.ssh.conn[id]` = `{ip, port, user, password}` maps straight onto the device
record. POST it when the row is saved:

```js
sshConnSave(id) {
  const c = (this.state.ssh.conn || {})[id] || {};
  SplitInference.api.updateDevice(id, {
    host: c.ip, port: Number(c.port) || 22, username: c.user,
    password: c.password || undefined,
    auth_method: c.password ? 'password' : 'key'
  });
}
```

The password is stored server-side in `secrets/credentials.json` and is **never**
returned — `GET /devices` only reports `has_password: true`. Keep doing what
`exportJson()` already does and leave credentials out of exported JSON.

---

## 2b. Broker / server card

The card's fields map straight onto `POST /server/config`. Keep the password in
component state only long enough to submit it — the backend encrypts it and
`GET /server/config` returns just `has_credentials`.

```js
saveServerConfig() {
  const f = this.state.server;           // {ip, port, api_port, user, password}
  SplitInference.api.saveServerConfig(f)
    .then(cfg => this.setState({
      server: { ...f, password: '' },    // don't retain it client-side
      serverSaved: cfg
    }))
    .catch(e => this.sshLog([{ text: '✗ ' + e.message, color: '#F87171' }]));
}

testServerConnection() {
  this.setState({ serverStatus: 'connecting' });
  SplitInference.api.testServerConnection()
    .then(r => {
      this.setState({ serverStatus: r.ok ? 'on' : 'error', serverTest: r });
      this.sshLog([{
        text: r.ok
          ? '✓ RabbitMQ ' + r.rabbitmq_version + ' · API ' + r.api
          : '✗ broker: ' + (r.broker_error || 'unreachable') + ' · API ' + r.api,
        color: r.ok ? '#4ade80' : '#F87171'
      }]);
    })
    .catch(e => this.setState({ serverStatus: 'error', serverError: e.message }));
}
```

Load the saved config on mount so the card repopulates (minus the password):

```js
SplitInference.api.getServerConfig().then(cfg => this.setState({
  server: { ip: cfg.ip, port: cfg.port, api_port: cfg.api_port, user: cfg.user, password: '' },
  serverStatus: cfg.status,
  serverHasCreds: cfg.has_credentials
}));
```

`ok` is true only when the AMQP handshake **and** the control-API health check
both succeed, so a green dot means the whole path works. Saving a new host resets
the status to `off` until re-tested.

---

## 3. Stream subscription

Open one stream when the component mounts:

```js
componentDidMount() {
  // ...existing code...
  this._stream = SplitInference.openStream({
    onSshStatus: (id, status) => this.sshSetStatus([id], status),
    onServerStatus: (status, frame) => this.setState({
      serverStatus: status, serverTest: frame
    }),
    onExecLine: (id, text, stream) => this.sshLog([{
      text: text, color: stream === 'stderr' ? '#F87171' : '#94A3B8'
    }]),
    onMetrics: (payload) => this.applyLiveMetrics(payload),
    onEvent: (frame) => this.sshLog([{ text: '· ' + frame.name, color: '#64748b' }]),
    onClose: () => this.setState({ streamUp: false }),
    onOpen: () => this.setState({ streamUp: true })
  });
}

componentWillUnmount() {
  // ...existing code...
  if (this._stream) this._stream.close();
}
```

The first frame is a `snapshot` replaying current SSH statuses, the broker/server
status, and the latest metrics per cluster, so the dots and gauges populate
immediately on reconnect.

---

## 4. Simulation / Pipeline tabs

Keep `simCluster()` for Simulate mode. In Live mode, store incoming payloads and
read from them instead:

```js
applyLiveMetrics(payload) {
  this.setState(s => ({
    live: { ...(s.live || {}), [payload.cluster]: SplitInference.toSimShape(payload) }
  }));
}

// One accessor for both modes -- every render site calls this instead of simCluster.
metricsFor(cl) {
  if (SplitInference.isLive()) return (this.state.live || {})[cl.id] || null;
  return this.simCluster(cl);
}
```

`live-patch.js` takes the shorter route and overrides `simCluster` itself,
delegating to the original in Simulate mode. Same result, but the timeline,
utilization bars, breakdown table, and the CSV/MD exports all pick up live
numbers without touching a single call site. Note that `toSimShape()` returning
`null` is a real answer (the cluster is idle), not a missing one — the patch
tracks which clusters have reported so it can tell "idle" from "no frame yet".

`toSimShape()` renames the §6 snake_case payload to the camelCase keys
`simCluster()` already returns (`edgeMs`, `transferMs`, `cloudMs`, `e2e`, `msg`,
`fps`, `edgeUtil`, …), plus `queueDepth` and `devices`, so the timeline,
utilization bars, and queue-depth rendering need no changes.

Seed the initial view from `GET /metrics/latest` when Live is switched on:

```js
startLive() {
  SplitInference.api.metricsLatest().then(body => {
    const live = {};
    body.clusters.forEach(p => { live[p.cluster] = SplitInference.toSimShape(p); });
    this.setState({ live, ran: true });
  });
}
```

Clusters with fewer than one edge and one cloud device come back as
`{cluster, idle: true, reason}` — `toSimShape()` returns `null` for those, which
matches what `simCluster()` already does for an idle cluster.

---

## 5. Pushing the inventory to the backend

The backend starts empty. Send the UI's own export shape once:

```js
pushInventory() {
  return SplitInference.api.seed(SplitInference.seedFromUiState(this.state));
}
```

`POST /seed` accepts `exportJson()` output verbatim (`{model, config, stages,
clusters}`) and also reads `clusterCfg` and `uploadedModel` if you include them.
Device specs land as `gflops` / `bandwidth_mb_s` / `latency_ms`; the response
echoes the resolved global config.

Connection details are not part of the export, so hosts stay blank until the
Control tab supplies them (or you pass `default_username` / `default_key_ref`).

---

## 6. Run buttons

```js
runFlowLive() {
  SplitInference.api.start(null)            // null -> every runnable cluster
    .then(r => this.setState({ active: 'pipeline', flowing: true, ran: true }))
    .catch(e => this.sshLog([{ text: '✗ ' + e.message, color: '#F87171' }]));
}
stopFlowLive() {
  SplitInference.api.stop(null).then(() => this.setState({ flowing: false }));
}
```

`POST /run/start` needs shards already deployed (`POST /run/deploy`) and a
reachable broker; it answers 503 if RabbitMQ is down and 400 if a cluster has no
edge/cloud pair. The response includes `predicted`, the simulator's own forecast
for the same cut, which is useful to show next to the live numbers.
