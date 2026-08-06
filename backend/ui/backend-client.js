/*
 * Transport bridge for the Split Inference Simulation Platform UI.
 *
 * Drop this in ahead of the DC logic and the mocked Control-tab handlers become
 * real calls. It deliberately contains no rendering logic: the UI keeps its own
 * state shape, and this only moves data.
 *
 *   <script src="backend-client.js"></script>
 *
 * Configure once from the browser console (persisted in localStorage):
 *   SplitInference.configure({ baseUrl: 'http://localhost:8000', token: 'dev-token-change-me' })
 *
 * Field names match the UI's state, so `bw` / `lat` / `cluster` are accepted and
 * returned alongside the canonical `bandwidth_mb_s` / `latency_ms` / `cluster_id`.
 */
(function (global) {
  'use strict';

  var LS_BASE = 'splitInference.baseUrl';
  var LS_TOKEN = 'splitInference.token';
  var LS_MODE = 'splitInference.mode'; // 'live' | 'simulate'

  function read(key, fallback) {
    try { return global.localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }
  function write(key, value) {
    try { global.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  var config = {
    baseUrl: read(LS_BASE, 'http://localhost:8000').replace(/\/+$/, ''),
    token: read(LS_TOKEN, ''),
    mode: read(LS_MODE, 'simulate')
  };

  function configure(opts) {
    if (!opts) return config;
    if (opts.baseUrl != null) { config.baseUrl = String(opts.baseUrl).replace(/\/+$/, ''); write(LS_BASE, config.baseUrl); }
    if (opts.token != null) { config.token = String(opts.token); write(LS_TOKEN, config.token); }
    if (opts.mode != null) { config.mode = opts.mode === 'live' ? 'live' : 'simulate'; write(LS_MODE, config.mode); }
    return config;
  }

  function isLive() { return config.mode === 'live'; }

  function headers(extra) {
    var h = extra || {};
    if (config.token) h['Authorization'] = 'Bearer ' + config.token;
    return h;
  }

  function request(method, path, body, opts) {
    var options = { method: method, headers: headers({}) };
    if (body instanceof FormData) {
      options.body = body; // let the browser set the multipart boundary
    } else if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    if (opts && opts.signal) options.signal = opts.signal;

    return fetch(config.baseUrl + path, options).then(function (res) {
      if (res.status === 204) return null;
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
        if (!res.ok) {
          var msg = (data && (data.detail || data.message)) || (res.status + ' ' + res.statusText);
          var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
          err.status = res.status;
          err.body = data;
          throw err;
        }
        return data;
      });
    });
  }

  var api = {
    // --- inventory -------------------------------------------------------
    listDevices: function () { return request('GET', '/devices'); },
    createDevice: function (device) { return request('POST', '/devices', device); },
    updateDevice: function (id, patch) { return request('PATCH', '/devices/' + encodeURIComponent(id), patch); },
    deleteDevice: function (id) { return request('DELETE', '/devices/' + encodeURIComponent(id)); },
    probeDevice: function (id, opts) {
      return request('POST', '/devices/' + encodeURIComponent(id) + '/probe', opts || {});
    },
    /* Auto-measure one device: convolution benchmark, timed transfer, TCP RTT.
       Reports the *solo* bandwidth -- the contended figure needs the fleet. */
    measureDevice: function (id, opts) {
      return request('POST', '/devices/' + encodeURIComponent(id) + '/measure', opts || {});
    },
    /* Auto-measure the fleet. Compute and latency run together; the transfer
       test runs one device at a time, because devices sharing an uplink would
       otherwise each report their share of it rather than their own capacity.
       With `contention` (default) a second pass runs them all at once and
       reports what each device actually gets during a run.
       opts: { device_ids, apply, contention, bandwidth_basis, iperf_server }.
       Minutes, not seconds, on a large fleet -- pass `signal` to allow a cancel. */
    measureFleet: function (opts) {
      return request('POST', '/devices/measure', opts || {});
    },

    // --- control tab -----------------------------------------------------
    /* `conn` is the UI's state.ssh.conn map: { [id]: {ip, port, user, password} } */
    connect: function (deviceIds, conn) {
      var credentials = (deviceIds || []).map(function (id) {
        var c = (conn && conn[id]) || {};
        return {
          device_id: id,
          ip: c.ip, port: c.port ? Number(c.port) : undefined,
          user: c.user, password: c.password || undefined
        };
      });
      return request('POST', '/control/connect', { device_ids: deviceIds, credentials: credentials });
    },
    disconnect: function (deviceIds) {
      return request('POST', '/control/disconnect', { device_ids: deviceIds || null });
    },
    /* `confirm` is required for destructive presets (reboot, systemctl
     * stop|restart) -- without it the call returns 409 with a message the UI
     * can show in a confirmation dialog. `$BROKER_IP` is substituted
     * server-side from the /server/config host. */
    /* `cwd` runs the command from a directory. Each command gets its own SSH
     * channel -- a fresh shell -- so a bare `cd` cannot persist between two
     * of them; the directory has to travel with every command. */
    /* Answers as soon as the command finishes OR after the server's
     * detach window, whichever comes first. A long run (`python3 Server.py`)
     * is reported in `running` and keeps streaming as exec_line frames --
     * no HTTP request is held open for the length of an experiment. */
    exec: function (deviceIds, command, confirm, cwd) {
      return request('POST', '/control/exec', {
        device_ids: deviceIds, command: command, confirm: !!confirm,
        cwd: cwd || undefined
      });
    },
    /* Ctrl-C. With no arguments it stops everything still running; pass
     * device ids to limit it to those hosts, or a job id for exactly one. */
    stopExec: function (deviceIds, jobId) {
      return request('POST', '/control/exec/stop', {
        device_ids: deviceIds || [], job_id: jobId || undefined
      });
    },
    listJobs: function () { return request('GET', '/control/jobs'); },
    listDirectories: function () { return request('GET', '/control/directories'); },
    saveDirectories: function (dirs) {
      return request('PUT', '/control/directories', { directories: dirs });
    },
    listPresets: function () { return request('GET', '/control/presets'); },
    savePresets: function (presets) { return request('PUT', '/control/presets', { presets: presets }); },
    resetPresets: function () { return request('POST', '/control/presets/reset'); },
    auditLog: function (limit) { return request('GET', '/control/audit?limit=' + (limit || 50)); },
    scp: function (deviceIds, file, remotePath) {
      var form = new FormData();
      form.append('file', file, file.name);
      form.append('device_ids', (deviceIds || []).join(','));
      form.append('remote_path', remotePath);
      return request('POST', '/control/scp', form);
    },
    /* Browse a device's filesystem over SFTP, so a path can be found before
     * pulling it. */
    remoteLs: function (deviceId, path) {
      return request('GET', '/control/ls?device_id=' + encodeURIComponent(deviceId) +
        '&path=' + encodeURIComponent(path || '.'));
    },
    /* Pull one file down and hand it to the browser as a save dialog. Fetched
     * rather than linked so the token travels in a header, not a query string
     * that would end up in logs and history. */
    pullFile: function (deviceId, path) {
      var url = config.baseUrl + '/control/fetch?device_id=' +
        encodeURIComponent(deviceId) + '&path=' + encodeURIComponent(path);
      return fetch(url, { headers: headers({}) }).then(function (res) {
        if (!res.ok) {
          return res.text().then(function (text) {
            var detail = text;
            try { detail = JSON.parse(text).detail || text; } catch (e) {}
            var err = new Error(detail);
            err.status = res.status;
            throw err;
          });
        }
        return res.blob().then(function (blob) {
          var name = (path || 'download').split('/').filter(Boolean).pop() || 'download';
          var href = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = href;
          a.download = name;
          document.body.appendChild(a);
          a.click();
          setTimeout(function () { a.remove(); URL.revokeObjectURL(href); }, 100);
          return { name: name, bytes: blob.size };
        });
      });
    },
    // --- result reports --------------------------------------------------
    /* Pull a finished run's result directory and chart it. Synchronous by
     * design -- it takes seconds, and a progress protocol for that is just a
     * second way for the UI and the server to disagree. */
    /* `window` is the optional static slice to analyse, `{start, end}` in
     * percent of the run's readings -- `{start: 5, end: 90}` charts batches 5
     * to 90 of a hundred. Omitted, the whole run is charted, which is what
     * every call did before the argument existed. */
    analyzeResults: function (deviceId, path, caseName, window) {
      var body = {
        device_id: deviceId, path: path, case_name: caseName || ''
      };
      if (window) {
        body.window_start = window.start;
        body.window_end = window.end;
      }
      return request('POST', '/reports/analyze', body);
    },
    listReports: function (limit) { return request('GET', '/reports?limit=' + (limit || 50)); },
    getReport: function (id) { return request('GET', '/reports/' + encodeURIComponent(id)); },
    saveReportNotes: function (id, notes, review) {
      return request('PUT', '/reports/' + encodeURIComponent(id) + '/notes',
        { notes: notes || {}, review: review == null ? '' : review });
    },
    deleteReport: function (id) { return request('DELETE', '/reports/' + encodeURIComponent(id)); },
    /* Chart PNGs are fetched, not linked: `<img src>` cannot carry the
     * Authorization header, and a token in the URL ends up in history and
     * server logs. Returns an object URL the caller owns and must revoke. */
    /* `version` busts the cache after a re-render.
     *
     * The PNGs are served with a long `max-age` because a chart under a given
     * name used to be immutable. Editing a chart's wording or hiding a series
     * re-draws it in place, so the name alone is no longer enough -- without the
     * stamp the browser keeps showing the chart from before the edit. */
    chartImage: function (id, file, version) {
      var url = config.baseUrl + '/reports/' + encodeURIComponent(id) +
        '/imgs/' + encodeURIComponent(file) +
        (version ? '?v=' + encodeURIComponent(version) : '');
      return fetch(url, { headers: headers({}) }).then(function (res) {
        if (!res.ok) throw new Error('chart image: ' + res.status);
        return res.blob().then(function (blob) { return URL.createObjectURL(blob); });
      });
    },

    saveReportViews: function (id, views) {
      return request('PUT', '/reports/' + encodeURIComponent(id) + '/views',
        { views: views });
    },

    sshStatus: function () { return request('GET', '/control/status'); },
    allowedCommands: function () { return request('GET', '/control/allowed-commands'); },

    // --- broker / server card -------------------------------------------
    /* `form` is the card's own shape: {ip, port, api_port, user, password}.
     * The password is write-only -- getServerConfig() returns has_credentials. */
    getServerConfig: function () { return request('GET', '/server/config'); },
    /* The card describes two machines: the control server you SSH into
     * (`ip`, `ssh_*`) and the broker (`amqp_host`, `port`, `user`), which is
     * frequently somewhere else entirely. Fields are forwarded only when
     * present -- the server treats an absent key as "leave it alone", so a
     * partial save cannot silently wipe the other half of the card. */
    saveServerConfig: function (form) {
      var body = {
        ip: form.ip,
        port: Number(form.port) || 5672,
        api_port: form.api_port ? Number(form.api_port) : undefined,
        user: form.user,
        password: form.password || undefined
      };
      if (form.amqp_host != null) body.amqp_host = form.amqp_host;
      if (form.ssh_port != null) body.ssh_port = Number(form.ssh_port) || 22;
      if (form.ssh_user != null) body.ssh_user = form.ssh_user;
      if (form.ssh_password) body.ssh_password = form.ssh_password;
      if (form.jump_enabled != null) body.jump_enabled = !!form.jump_enabled;
      return request('POST', '/server/config', body);
    },
    /* SSH only -- logs in and runs `uname -a`. What you want when the question
     * is "can I get a shell on this box", which has nothing to do with the
     * broker. */
    testServerSsh: function () { return request('POST', '/server/ssh/test'); },
    /* SSH + AMQP + control API. Only relevant once you are running split
     * inference and the devices need a broker to publish to. */
    testServerConnection: function () { return request('POST', '/server/test'); },

    // --- clusters + config ----------------------------------------------
    listClusters: function () { return request('GET', '/clusters'); },
    saveCluster: function (cluster) { return request('POST', '/clusters', cluster); },
    patchCluster: function (id, patch) { return request('PATCH', '/clusters/' + id, patch); },
    getConfig: function () { return request('GET', '/config'); },
    patchConfig: function (patch) { return request('PATCH', '/config', patch); },
    listModels: function () { return request('GET', '/models'); },
    saveModel: function (model) { return request('POST', '/models', model); },

    // --- run lifecycle ---------------------------------------------------
    deploy: function (clusterId, opts) {
      return request('POST', '/run/deploy', Object.assign({ cluster: clusterId }, opts || {}));
    },
    start: function (clusterId, opts) {
      return request('POST', '/run/start', Object.assign({ cluster: clusterId }, opts || {}));
    },
    stop: function (clusterId) { return request('POST', '/run/stop', { cluster: clusterId }); },
    activeRuns: function () { return request('GET', '/run/active'); },
    runHistory: function (limit) { return request('GET', '/run/history?limit=' + (limit || 25)); },

    // --- metrics + persistence -------------------------------------------
    metricsLatest: function () { return request('GET', '/metrics/latest'); },
    seed: function (uiExport) { return request('POST', '/seed', uiExport); },
    exportInventory: function () { return request('GET', '/export'); },
    health: function () { return request('GET', '/health'); }
  };

  /*
   * WebSocket stream with backoff reconnect.
   *
   *   var stream = SplitInference.openStream({
   *     onSshStatus: function (deviceId, status) {...},
   *     onServerStatus: function (status, frame) {...},   // broker/API dot
   *     onExecLine:  function (deviceId, text, stream) {...},
   *     onMetrics:   function (payload) {...},
   *     onSnapshot:  function (snap) {...},
   *     onEvent:     function (frame) {...},
   *     onOpen: fn, onClose: fn
   *   });
   *   stream.close();   // stops reconnecting too
   */
  function openStream(handlers) {
    handlers = handlers || {};
    var closed = false;
    var socket = null;
    var attempt = 0;
    var timer = null;

    function url() {
      var http = config.baseUrl;
      var ws = http.replace(/^http/, 'ws');
      return ws + '/ws/stream?token=' + encodeURIComponent(config.token);
    }

    function schedule() {
      if (closed) return;
      attempt += 1;
      var delay = Math.min(30000, 500 * Math.pow(2, Math.min(attempt, 6)));
      timer = global.setTimeout(open, delay);
    }

    function open() {
      if (closed) return;
      try { socket = new global.WebSocket(url()); } catch (e) { schedule(); return; }

      socket.onopen = function () {
        attempt = 0;
        if (handlers.onOpen) handlers.onOpen();
      };
      socket.onclose = function (ev) {
        if (handlers.onClose) handlers.onClose(ev);
        schedule();
      };
      socket.onerror = function () { /* onclose always follows */ };
      socket.onmessage = function (ev) {
        var frame;
        try { frame = JSON.parse(ev.data); } catch (e) { return; }
        switch (frame.type) {
          case 'ping': return;
          case 'snapshot':
            if (handlers.onSnapshot) handlers.onSnapshot(frame);
            if (handlers.onSshStatus && frame.ssh_status) {
              Object.keys(frame.ssh_status).forEach(function (id) {
                handlers.onSshStatus(id, frame.ssh_status[id]);
              });
            }
            if (handlers.onServerStatus && frame.server_status) {
              handlers.onServerStatus(frame.server_status, frame);
            }
            if (handlers.onMetrics && frame.metrics) frame.metrics.forEach(handlers.onMetrics);
            return;
          case 'ssh_status':
            if (handlers.onSshStatus) handlers.onSshStatus(frame.device_id, frame.status, frame.detail);
            return;
          case 'server_status':
            if (handlers.onServerStatus) handlers.onServerStatus(frame.status, frame);
            return;
          case 'exec_line':
            if (handlers.onExecLine) handlers.onExecLine(frame.device_id, frame.text, frame.stream);
            return;
          case 'metrics':
            if (handlers.onMetrics) handlers.onMetrics(frame.payload);
            return;
          default:
            if (handlers.onEvent) handlers.onEvent(frame);
        }
      };
    }

    open();
    return {
      close: function () {
        closed = true;
        if (timer) global.clearTimeout(timer);
        if (socket) { try { socket.close(); } catch (e) {} }
      },
      get readyState() { return socket ? socket.readyState : 3; }
    };
  }

  /* Map a §6 metrics payload onto the camelCase keys `simCluster()` returns, so
   * existing render code can consume a live frame unchanged. */
  function toSimShape(payload) {
    if (!payload || payload.idle) return null;
    var bottleneck = Math.max(payload.edge_ms || 0, payload.transfer_ms || 0, payload.cloud_ms || 0) || 1;
    return {
      cut: payload.cut,
      layerCount: payload.layer_count,
      edgeMs: payload.edge_ms,
      transferMs: payload.transfer_ms,
      cloudMs: payload.cloud_ms,
      e2e: payload.e2e_ms,
      msg: payload.msg_mb,
      fps: payload.fps,
      bottleneck: bottleneck,
      edgeUtil: payload.edge_util,
      transferUtil: payload.transfer_util,
      cloudUtil: payload.cloud_util,
      edgeG: payload.edge_gflops,
      cloudG: payload.cloud_gflops,
      bw: payload.bandwidth_mb_s,
      lat: payload.latency_ms,
      guardHit: !!payload.guard_hit,
      queueDepth: payload.queue_depth,
      devices: payload.devices || [],
      source: payload.source
    };
  }

  /* Build the /seed body from the UI's own exportJson() output. */
  function seedFromUiState(state) {
    return {
      model: state.config && state.config.modelName,
      config: state.config,
      stages: state.stages || [],
      clusters: [],
      clusterCfg: state.clusterCfg || {},
      uploadedModel: state.uploadedModel || null,
      replace: true
    };
  }

  global.SplitInference = {
    configure: configure,
    config: config,
    isLive: isLive,
    api: api,
    openStream: openStream,
    toSimShape: toSimShape,
    seedFromUiState: seedFromUiState
  };
})(typeof window !== 'undefined' ? window : this);
