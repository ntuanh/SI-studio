/*
 * Live bridge for the Split Inference Studio UI (guide §8).
 *
 * `tools/build_web.py` appends this file to the end of the page's
 * `<script type="text/x-dc">` block, after `class Component extends DCLogic`.
 * That script is evaluated by the DC runtime as
 *
 *     new Function('DCLogic', 'StreamableLogic', 'React', src + ';return Component')
 *
 * so `Component` is in scope here and patching its prototype is the whole
 * mechanism -- no rewriting of the original method bodies, which keeps this
 * file readable against the guide and keeps a UI re-export from silently
 * losing the wiring (the build re-applies it from source every time).
 *
 * Nothing here changes the UI's state shape or its rendering. Simulate mode
 * runs the untouched in-browser math; Live mode routes the same call sites
 * through the FastAPI control plane and paints the frames it pushes back.
 */
(function () {
  'use strict';

  var SI = window.SplitInference;
  if (!SI || typeof Component !== 'function') {
    console.warn('[live] backend-client.js not loaded — Simulate mode only');
    return;
  }

  /* The control server's reserved target id. It is a real SSH host but not a
   * device: it has no GFLOPS and belongs to no cluster, so it is kept out of
   * `state.stages` and injected into the Targets list at render time. */
  var SERVER_ID = '__server__';

  /* Shown in the password box when the backend holds one but will not return
   * it. Sent back unchanged it would overwrite the real password with this
   * literal, so every caller strips it before submitting. */
  var STORED_PASSWORD = '•••••••• (saved)';

  var P = Component.prototype;
  var base = {
    componentDidMount: P.componentDidMount,
    componentDidUpdate: P.componentDidUpdate,
    componentWillUnmount: P.componentWillUnmount,
    renderVals: P.renderVals,
    simCluster: P.simCluster,
    runSim: P.runSim,
    runFlow: P.runFlow,
    sshConnectAll: P.sshConnectAll,
    sshRun: P.sshRun,
    sshScp: P.sshScp,
    sshServerTest: P.sshServerTest,
    removeStage: P.removeStage,
    removeDevice: P.removeDevice
  };

  // Console palette, matching the colors the mocked handlers used.
  var C = {
    cmd: '#e2e8f0', head: '#a78bfa', body: '#94A3B8', ok: '#4ade80',
    err: '#F87171', info: '#22D3EE', warn: '#FBBF24', hint: '#64748b'
  };

  function lineColor(text, stream) {
    if (stream === 'stderr') return C.err;
    var first = text.charAt(0);
    if (stream === 'meta') {
      if (first === '┌') return C.head;   // ┌─ device banner
      if (first === '⚠') return C.warn;   // ⚠ destructive
      return C.info;
    }
    if (first === '✓') return C.ok;       // ✓
    if (text.indexOf('└─ done') === 0) return C.ok;
    return C.body;
  }

  function errText(e) {
    return (e && e.message) ? e.message : String(e);
  }

  /* Compare a preset label to a name we are looking for.
   *
   * The labels are typed by hand into the preset editor, so "Run Stage 1" and
   * "run  stage 1" have to be the same handle as "run stage 1" -- matching them
   * literally would make the feature look broken for a capital letter. */
  function normLabel(s) {
    return String(s == null ? '' : s).trim().toLowerCase().replace(/\s+/g, ' ');
  }

  /* Round a measured value for the device card's number inputs.
   *
   * The measurement carries more precision than the form can usefully show --
   * 112.43718 MB/s in a 60px box is noise, and the simulator's own output is
   * quoted to far fewer digits than that anyway. */
  function spec(value, places) {
    var f = Math.pow(10, places);
    return Math.round(value * f) / f;
  }

  /* "conv-fp32 · sftp-blob · tcp-connect" -- how each number was arrived at.
   *
   * Worth a line in the console because the three specs have very different
   * confidence: a measured convolution and a vendor peak table both land in
   * the same box, and only this says which one you got. */
  /* The line under a stage header saying how the last measurement went.
   *
   * Names the devices rather than only counting them: "8 of 9" sends you to
   * the console to find out which one, which is the trip this line exists to
   * save. Successes are named too when there are few enough to fit -- on a
   * nine-machine stage the count is the useful summary and the failures are
   * the detail worth spelling out. */
  var NOTICE_NAME_LIMIT = 4;

  function nameList(names) {
    if (names.length <= NOTICE_NAME_LIMIT) return names.join(', ');
    return names.slice(0, NOTICE_NAME_LIMIT).join(', ') +
      ' +' + (names.length - NOTICE_NAME_LIMIT) + ' more';
  }

  function noticeStyle(color) {
    return {
      borderLeft: '3px solid ' + color, background: 'var(--bg)',
      borderRadius: '7px', padding: '6px 9px', fontSize: '11px',
      fontWeight: 600, color: color, lineHeight: 1.4
    };
  }

  function sourceList(sources) {
    var s = sources || {};
    var parts = ['gflops', 'bandwidth', 'latency'].map(function (k) { return s[k]; })
      .filter(function (v) { return !!v; });
    return parts.length ? parts.join(' · ') : 'no source reported';
  }

  /* Typed in the command box to interrupt whatever is still running.
   *
   * A long command outlives the request that started it -- `python3 Server.py`
   * runs for the length of the experiment, while /control/exec answers in
   * seconds -- so by the time you want to stop it there is no spinner left to
   * cancel and no shell session to press Ctrl-C in. This box is the only place
   * left to type it, so it accepts the things someone would actually try. */
  var INTERRUPT_RE = /^(\^c|ctrl[-+ ]?c|sigint)$/i;

  /* Drop the "a password is on file" marker before sending, so submitting an
   * untouched form leaves the stored password alone instead of replacing it
   * with the placeholder text. */
  function realCreds(conn) {
    var out = {};
    Object.keys(conn || {}).forEach(function (id) {
      var c = conn[id] || {};
      out[id] = c.password === STORED_PASSWORD
        ? Object.assign({}, c, { password: '' })
        : c;
    });
    return out;
  }

  /* ---------------------------------------------------------------- session
   *
   * The page mints fresh random device ids on every load (`defaultStages()`),
   * so without this nothing survives a refresh: names, specs and connection
   * details reset, and every sync registers a *new* set of devices on the
   * backend while the old ones linger. Persisting the inventory makes the ids
   * stable, which is what everything else keys off.
   *
   * Passwords are deliberately excluded. They live in the backend's encrypted
   * secret store; localStorage is plain text readable by any script on the
   * origin, and a saved SSH password there would outlive the tab.
   */
  var LS_STATE = 'splitInference.uiState';
  var STATE_VERSION = 1;

  function saveSession(state) {
    try {
      var conn = {};
      Object.keys(state.ssh.conn || {}).forEach(function (id) {
        var c = state.ssh.conn[id] || {};
        if (!c.ip && !c.user) return;
        conn[id] = { ip: c.ip || '', port: c.port || 22, user: c.user || '' };
      });
      var sv = state.ssh.server || {};
      localStorage.setItem(LS_STATE, JSON.stringify({
        v: STATE_VERSION,
        stages: state.stages,
        config: state.config,
        clusterCfg: state.clusterCfg,
        uploadedModel: state.uploadedModel,
        // The Visual panel's inputs, not its results: charts and notes live in
        // the report folder on the backend, and re-hydrating a stale copy of
        // them here would fight with whatever the server holds.
        viz: {
          dir: (state.viz || {}).dir || '',
          caseName: (state.viz || {}).caseName || '',
          // The window is an input like the other two: an operator trimming
          // warm-up off one run is about to do it to the next one as well.
          // Left uncoerced -- `undefined` drops out of the JSON and comes back
          // as the default, where `''` is a box that was deliberately cleared.
          windowOn: !!(state.viz || {}).windowOn,
          windowStart: (state.viz || {}).windowStart,
          windowEnd: (state.viz || {}).windowEnd
        },
        ssh: {
          conn: conn,
          cwd: state.ssh.cwd || '',
          command: state.ssh.command || '',
          // Which machines were being worked on, and which console was open.
          // Cheap to store and tedious to re-pick every reload.
          selected: state.ssh.selected || [],
          focus: state.ssh.focus || '',
          pullPath: state.ssh.pullPath || '',
          server: {
            ip: sv.ip, port: sv.port, user: sv.user, jump: !!sv.jump,
            amqpHost: sv.amqpHost || '', amqpPort: sv.amqpPort, amqpUser: sv.amqpUser,
            expanded: sv.expanded !== false, showBroker: !!sv.showBroker
          }
        }
      }));
    } catch (e) { /* quota or private mode: not worth interrupting anything */ }
  }

  function loadSession() {
    try {
      var raw = localStorage.getItem(LS_STATE);
      if (!raw) return null;
      var saved = JSON.parse(raw);
      return (saved && saved.v === STATE_VERSION && saved.stages) ? saved : null;
    } catch (e) { return null; }
  }

  /* Placeholder while a chart's blob is still being fetched. An empty `src`
   * makes some browsers re-request the page itself and draw a broken-image
   * glyph; a 1x1 transparent GIF just shows the card's background. */
  var BLANK_IMG = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

  /* Which history bucket a report belongs to. The server sends `created_at`
   * as an ISO timestamp, so the day is its first ten characters -- parsed as a
   * Date it would be shifted by the browser's timezone and a late-evening run
   * could land on the wrong chip. */
  function vizDay(report) {
    return (report && report.created_at || '').slice(0, 10);
  }

  /* ---- the analysis window: chart part of a run instead of all of it ------
   *
   * The first batches of a run are warm-up and the last are the tail draining,
   * and both drag every mean and every axis around. The window is the operator
   * saying "batches 5 to 90 of a hundred" once, before analysing, so the whole
   * report is drawn from the same stretch.
   *
   * Static by design (see app/reports/window.py): the bounds are typed, stored
   * with the report, and re-used verbatim whenever its charts are re-drawn. */
  var VIZ_WINDOW_DEFAULT = { windowStart: '5', windowEnd: '90' };

  /* What a window box currently holds. `undefined` is a box nobody has touched
   * and takes the default; `''` is one that was deliberately cleared and stays
   * empty, so the field does not refill itself under the cursor. */
  function vizWindowText(viz, key) {
    var value = (viz || {})[key];
    return value == null ? VIZ_WINDOW_DEFAULT[key] : String(value);
  }

  /* `null` when the window is off, `{start, end}` when it is usable, and
   * `{error}` when the two boxes do not describe a slice of a run.
   *
   * Validated here as well as on the backend because the backend's answer is a
   * 422 after an SSH pull that need never have happened -- and because the
   * message can name what was typed. */
  function vizWindowOf(viz) {
    if (!viz || !viz.windowOn) return null;
    var start = parseFloat(vizWindowText(viz, 'windowStart'));
    var end = parseFloat(vizWindowText(viz, 'windowEnd'));
    if (isNaN(start) || isNaN(end)) {
      return { error: 'the window needs two numbers — 5 and 90 charts batches 5 to 90 of 100' };
    }
    if (start < 0 || end > 100) {
      return { error: 'the window is a percentage of the run, so both ends sit in 0–100' };
    }
    if (start >= end) {
      return { error: 'the window runs from ' + fmtPct(start) + '% to ' + fmtPct(end) +
        '% — the start has to come first' };
    }
    return { start: start, end: end };
  }

  function fmtPct(n) {
    return String(Math.round(n * 100) / 100);
  }

  function vizWindowLabel(w) {
    return w && !w.error ? fmtPct(w.start) + '–' + fmtPct(w.end) + '%' : '';
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  /* Compare mode's column accents, in slot order. Three, because three is the
   * limit: a fourth column drawn on a 1400px page is 300px of chart, which is
   * narrower than the figures were rendered for. */
  var VIZ_SLOT = ['var(--edge)', 'var(--cloud)', 'var(--server)'];
  var VIZ_SLOT_MARK = ['①', '②', '③'];
  var VIZ_COMPARE_MAX = 3;

  /* A chart PNG's pixel size, measured off the blob as it arrives.
   *
   * Compare mode needs it: the guide draws a figure as wide as its categories
   * need, so the same chart is 1829px across in one run and 2271px in another.
   * Scaled to a shared column width those two come out at different scales,
   * and a bar that is 25% taller for no reason but the rendering is exactly the
   * misreading the mode exists to prevent. Knowing the shape lets every cell in
   * a row be drawn into the same box instead (see `figStyle`).
   *
   * Resolves null rather than rejecting on a broken image: a missing size means
   * "lay it out the ordinary way", which is a fine answer. */
  function imageShape(src) {
    return new Promise(function (resolve) {
      if (!src) return resolve(null);
      var probe = new Image();
      probe.onload = function () {
        resolve(probe.naturalWidth && probe.naturalHeight
          ? { w: probe.naturalWidth, h: probe.naturalHeight } : null);
      };
      probe.onerror = function () { resolve(null); };
      probe.src = src;
    });
  }

  /* The row order for compare mode: every chart in any of the picked reports,
   * once each, in catalogue order.
   *
   * Keyed on the chart's own id rather than its position, because position is
   * exactly what cannot be trusted here -- a run with no `map.log` is missing
   * charts 09 and 10, and lining two galleries up by index would put its 08
   * next to the other's 09 and invite a reading of the difference that is
   * really a reading of the misalignment. The catalogue number (`file`'s first
   * two characters) is stable across re-runs, which is what makes it the sort
   * key; `seq` only breaks ties, so reports whose numbering has drifted still
   * come out in a fixed order rather than a hash-dependent one. */
  function vizCompareSpine(ids, reports) {
    var seen = {};
    var out = [];
    (ids || []).forEach(function (rid) {
      (((reports || {})[rid] || {}).charts || []).forEach(function (c) {
        if (seen[c.id]) return;
        seen[c.id] = 1;
        out.push({
          id: c.id, title: c.title || c.id, kind: c.kind || '',
          key: (c.file || '').slice(0, 2), seq: out.length
        });
      });
    });
    out.sort(function (a, b) {
      if (a.key !== b.key) return a.key < b.key ? -1 : 1;
      return a.seq - b.seq;
    });
    return out;
  }

  /* Whether the Control tab should hit the backend.
   *
   * Deliberately NOT the Live/Simulate toggle. That toggle chooses where the
   * *inference numbers* come from -- in-browser math or measured frames -- and
   * both are legitimate answers. "Did my SSH password work" has no simulated
   * answer worth giving: the built-in mock replies `✓ broker reachable ·
   * RabbitMQ 3.13` after 700ms no matter what you typed, which reads as
   * confirmation that a wrong password worked. So anything that touches a real
   * machine goes to the backend whenever one is reachable.
   *
   * Unknown (the health check has not landed yet) counts as reachable: the
   * request will report its own failure far more accurately than a guess here.
   */
  function controlLive(self) {
    return !!SI.config.baseUrl && self.state.siReachable !== false;
  }

  /* Collapse a line that repeats back-to-back into `… ×N`.
   *
   * Fanning one command across a dozen devices, or reconnecting a few times,
   * fills the console with identical lines and buries the output that is
   * actually being looked for. Only *consecutive* repeats collapse, so
   * interleaved per-device results are never merged. */
  function appendCollapsed(out, lines) {
    var next = out.slice();
    lines.forEach(function (line) {
      var last = next[next.length - 1];
      var base = last && (last.baseText || last.text);
      if (last && base === line.text && last.color === line.color) {
        var n = (last.repeat || 1) + 1;
        next[next.length - 1] = {
          text: line.text + '   ×' + n, color: line.color, baseText: line.text, repeat: n
        };
        return;
      }
      next.push(line);
    });
    return next.slice(-300);
  }

  /* Keep the console pinned to the newest line, unless the operator has
   * scrolled up to read something.
   *
   * "Are we at the bottom" cannot be answered *after* the update: by then the
   * content has already grown, so a batch of thirty lines always looks far
   * from the bottom and following would never happen. So intent is tracked as
   * the user scrolls instead, and the update just honours it. */
  var lastFocus = null;

  function followConsoleTail(focus) {
    var box = document.getElementById('si-console');
    if (!box) return;

    // Switching target replaces the contents wholesale: start that stream at
    // its newest line rather than inheriting the previous one's scroll offset.
    if (focus !== lastFocus) {
      lastFocus = focus;
      box.dataset.siPinned = '1';
      box.scrollTop = box.scrollHeight;
      return;
    }

    if (!box.dataset.siPinBound) {
      box.dataset.siPinBound = '1';
      box.dataset.siPinned = '1';
      box.addEventListener('scroll', function () {
        var gap = box.scrollHeight - box.scrollTop - box.clientHeight;
        // Re-pins by itself when scrolled back down to the end.
        box.dataset.siPinned = gap < 40 ? '1' : '0';
      });
    }

    if (box.dataset.siPinned === '1') box.scrollTop = box.scrollHeight;
  }

  /* ---------------------------------------------------------- pointer effects
   *
   * A light that trails the cursor, and the accent it hands to whatever control
   * it is over. The blink itself is pure CSS (`siBlink`, added by
   * tools/build_web.py); what needs script is the position and the colour.
   *
   * Everything lives outside React: two `pointer-events:none` divs appended to
   * <body>, moved by transform, plus three classes on <body>. Nothing here
   * touches component state -- at 60Hz a `setState` per frame would re-render
   * the whole dashboard to move a circle, and the render is expensive enough
   * that the light would visibly lag the pointer it is meant to be following.
   *
   * Returns its own teardown, or null when the effects are not wanted: no
   * pointer that can hover, or an operator who has asked for less motion.
   */
  var FX_ACCENT_SKIP = /^(rgba?\(0, 0, 0, 0\)|transparent)$/;

  /* What colour the light should take from the control under it.
   *
   * A filled button (Run, Connect, reboot) says its accent in its background
   * and paints its label white; a bare one (`select all`, the preset chips)
   * says it in the text colour. Reading background first and falling back to
   * colour therefore gets the meaningful hue in both cases -- and never white
   * on white, which is what taking `color` alone would give on every primary
   * button on the page. */
  function fxAccent(el) {
    for (var node = el; node && node !== document.body; node = node.parentElement) {
      var cs = window.getComputedStyle(node);
      if (!FX_ACCENT_SKIP.test(cs.backgroundColor)) return cs.backgroundColor;
      if (node.tagName === 'BUTTON' || node.tagName === 'A') return cs.color;
    }
    return '';
  }

  function installPointerFx() {
    var fine = window.matchMedia('(hover: hover) and (pointer: fine)');
    var still = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (!fine.matches || still.matches) return null;

    var glow = document.createElement('div');
    glow.className = 'si-cursor-glow';
    var dot = document.createElement('div');
    dot.className = 'si-cursor-dot';
    document.body.appendChild(glow);
    document.body.appendChild(dot);

    var body = document.body;
    var x = 0, y = 0, frame = 0, seen = null;

    // One rAF per frame at most, however many move events arrive in it: the
    // browser coalesces nothing here, and a laid-out transform per event is
    // the one thing in this file that can cost frames.
    var paint = function () {
      frame = 0;
      var t = 'translate3d(' + x + 'px, ' + y + 'px, 0)';
      glow.style.transform = t;
      dot.style.transform = t;
    };

    var onMove = function (e) {
      x = e.clientX;
      y = e.clientY;
      if (!frame) frame = window.requestAnimationFrame(paint);
      body.classList.add('si-fx-on');

      // Recomputed only when the pointer crosses into a different element --
      // `getComputedStyle` walks the cascade, and doing that per frame while
      // the pointer sweeps one button is wasted work.
      var el = e.target;
      if (el === seen) return;
      seen = el;

      // `cursor: pointer` as the definition of clickable: it already marks
      // every one of them, including the target rows and preset chips, which
      // are click-handled divs and match no selector for "button".
      var hot = el && el.nodeType === 1 &&
        window.getComputedStyle(el).cursor === 'pointer';
      body.classList.toggle('si-fx-hot', !!hot);
      var accent = hot ? fxAccent(el) : '';
      if (accent) body.style.setProperty('--si-fx', accent);
      else body.style.removeProperty('--si-fx');
    };

    // Parked outside the window the light is stale, and left visible it says
    // the pointer is somewhere it is not.
    var onLeave = function (e) {
      if (e && e.relatedTarget) return;   // still inside, just crossing elements
      body.classList.remove('si-fx-on', 'si-fx-hot', 'si-fx-tap');
      seen = null;
    };
    var onDown = function () { body.classList.add('si-fx-tap'); };
    var onUp = function () { body.classList.remove('si-fx-tap'); };

    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('mouseout', onLeave, true);
    document.addEventListener('mousedown', onDown, true);
    document.addEventListener('mouseup', onUp, true);
    window.addEventListener('blur', onLeave);

    return function () {
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseout', onLeave, true);
      document.removeEventListener('mousedown', onDown, true);
      document.removeEventListener('mouseup', onUp, true);
      window.removeEventListener('blur', onLeave);
      if (frame) window.cancelAnimationFrame(frame);
      body.classList.remove('si-fx-on', 'si-fx-hot', 'si-fx-tap');
      body.style.removeProperty('--si-fx');
      glow.remove();
      dot.remove();
    };
  }

  /* Preset editor: one `label = command` per line.
   *
   * A line-based textarea rather than a row-per-preset form because that is
   * how these lists are actually maintained -- paste a few in, reorder, delete
   * three at once. A form with add/remove buttons is more chrome for less. */
  function openPresetEditor(presets, dirs, onSave, onReset) {
    var existing = document.getElementById('si-presets');
    if (existing) existing.remove();

    var wrap = document.createElement('div');
    wrap.id = 'si-presets';
    wrap.style.cssText = 'position:fixed; inset:0; z-index:9000; background:rgba(15,23,42,.55);' +
      'display:flex; align-items:center; justify-content:center; font-family:inherit;';

    var card = document.createElement('div');
    card.style.cssText = 'background:var(--raised); color:var(--ink); border:1px solid var(--border);' +
      'border-radius:14px; padding:20px; width:min(620px, 94vw); box-shadow:0 18px 48px rgba(0,0,0,.35);' +
      'display:flex; flex-direction:column; gap:12px;';

    var title = document.createElement('div');
    title.textContent = 'Commands & directories';
    title.style.cssText = 'font-size:15px; font-weight:700;';
    card.appendChild(title);

    function section(heading, note, value, minHeight) {
      var h = document.createElement('div');
      h.textContent = heading;
      h.style.cssText = 'font-size:10px; text-transform:uppercase; letter-spacing:.05em;' +
        'color:var(--muted); font-weight:700; margin-top:4px;';
      card.appendChild(h);

      var n = document.createElement('div');
      n.innerHTML = note;
      n.style.cssText = 'font-size:11px; line-height:1.6; color:var(--muted);';
      card.appendChild(n);

      var ta = document.createElement('textarea');
      ta.value = value;
      ta.spellcheck = false;
      ta.style.cssText = 'min-height:' + minHeight + 'px; resize:vertical; background:var(--bg);' +
        'border:1px solid var(--border); border-radius:9px; padding:10px; color:var(--ink);' +
        'font-family:ui-monospace, monospace; font-size:12px; line-height:1.7; outline:none;';
      card.appendChild(ta);
      return ta;
    }

    var area = section(
      'Commands',
      'One per line, <code>label = command</code>. Saved commands become runnable ' +
        'even if they are not on the built-in allow-list.<br>' +
        '<code>cd</code> does not belong here — each command runs in its own shell.',
      presets.map(function (p) {
        return (p.label && p.label !== p.command) ? p.label + ' = ' + p.command : p.command;
      }).join('\n'),
      170
    );

    var dirArea = section(
      'Directories',
      'One per line, <code>label = path</code>. Click a chip to run everything ' +
        'from there; the label defaults to the last path segment.',
      (dirs || []).map(function (d) {
        var tail = d.path.split('/').filter(Boolean).pop();
        return (d.label && d.label !== tail) ? d.label + ' = ' + d.path : d.path;
      }).join('\n'),
      110
    );

    var row = document.createElement('div');
    row.style.cssText = 'display:flex; gap:8px; align-items:center;';
    function button(text, css) {
      var b = document.createElement('button');
      b.textContent = text;
      b.style.cssText = 'border-radius:9px; padding:8px 16px; font-size:12px; font-weight:700;' +
        'cursor:pointer; border:1px solid var(--border); ' + css;
      row.appendChild(b);
      return b;
    }
    var reset = button('Restore defaults', 'background:transparent; color:var(--muted);');
    var spacer = document.createElement('div');
    spacer.style.cssText = 'flex:1;';
    row.appendChild(spacer);
    var cancel = button('Cancel', 'background:var(--surface); color:var(--ink);');
    var save = button('Save', 'background:var(--edge); color:#fff; border-color:transparent;');
    card.appendChild(row);

    function close() { wrap.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    cancel.onclick = close;
    wrap.onclick = function (e) { if (e.target === wrap) close(); };
    reset.onclick = function () { close(); onReset(); };
    /* Split `label = value`, but only when the left side looks like a label:
     * short, and free of shell syntax. Otherwise `FOO=bar cmd` would lose its
     * variable assignment, and `find . -name '*=*'` would be mangled. */
    function parseLines(text, valueKey, fallbackLabel) {
      return text.split('\n').map(function (line) {
        var trimmed = line.trim();
        if (!trimmed) return null;
        var eq = trimmed.indexOf('=');
        var item = {};
        if (eq > 0 && eq < 28 && !/[|;&"'$]/.test(trimmed.slice(0, eq))) {
          item.label = trimmed.slice(0, eq).trim();
          item[valueKey] = trimmed.slice(eq + 1).trim();
        } else {
          item.label = fallbackLabel(trimmed);
          item[valueKey] = trimmed;
        }
        return item[valueKey] ? item : null;
      }).filter(Boolean);
    }

    save.onclick = function () {
      var parsed = parseLines(area.value, 'command', function (t) {
        return t.split(/\s+/)[0].slice(0, 24);
      });
      var parsedDirs = parseLines(dirArea.value, 'path', function (t) {
        return t.split('/').filter(Boolean).pop() || t;
      });
      close();
      onSave(parsed, parsedDirs);
    };

    wrap.appendChild(card);
    document.body.appendChild(wrap);
    area.focus();
  }

  /* Only reached with no backend at all. Say so loudly -- the canned output
   * below it is indistinguishable from the real thing otherwise. */
  function mockNotice(self, what) {
    self.sshLog([{
      text: '⚠ no backend at ' + (SI.config.baseUrl || '(unset)') + ' — ' + what +
        ' is SIMULATED; nothing was contacted',
      color: C.warn
    }]);
  }

  // ------------------------------------------------------------ settings modal
  /* Built from plain DOM rather than template markup: it is operator plumbing
   * (where is the backend, what token), not part of the product surface, and
   * this way the template edit stays down to one header group. It inherits the
   * page's CSS variables, so it follows the light/dark toggle. */
  function openSettings(onSaved) {
    var existing = document.getElementById('si-settings');
    if (existing) existing.remove();

    var wrap = document.createElement('div');
    wrap.id = 'si-settings';
    wrap.style.cssText = 'position:fixed; inset:0; z-index:9000; background:rgba(15,23,42,.55);' +
      'display:flex; align-items:center; justify-content:center; font-family:inherit;';

    var card = document.createElement('div');
    card.style.cssText = 'background:var(--raised); color:var(--ink); border:1px solid var(--border);' +
      'border-radius:14px; padding:20px; width:min(420px, 92vw); box-shadow:0 18px 48px rgba(0,0,0,.35);' +
      'display:flex; flex-direction:column; gap:12px;';

    function field(label, value, type, hint) {
      var box = document.createElement('label');
      box.style.cssText = 'display:flex; flex-direction:column; gap:5px; font-size:11px;' +
        'text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:700;';
      box.appendChild(document.createTextNode(label));
      var input = document.createElement('input');
      input.type = type || 'text';
      input.value = value || '';
      input.style.cssText = 'background:var(--bg); border:1px solid var(--border); border-radius:9px;' +
        'padding:9px 11px; font-size:13px; color:var(--ink); font-family:ui-monospace, monospace;' +
        'text-transform:none; letter-spacing:0; outline:none;';
      box.appendChild(input);
      if (hint) {
        var h = document.createElement('span');
        h.textContent = hint;
        h.style.cssText = 'font-size:10px; font-weight:500; text-transform:none; letter-spacing:0; color:var(--muted);';
        box.appendChild(h);
      }
      card.appendChild(box);
      return input;
    }

    var title = document.createElement('div');
    title.textContent = 'Backend connection';
    title.style.cssText = 'font-size:15px; font-weight:700;';
    card.appendChild(title);

    var urlInput = field('Control plane URL', SI.config.baseUrl, 'text',
      'Leave blank to use this page’s own origin.');
    var tokenInput = field('API token', SI.config.token, 'password',
      'Sent as Authorization: Bearer on every request.');

    var row = document.createElement('div');
    row.style.cssText = 'display:flex; gap:8px; justify-content:flex-end; margin-top:4px;';
    var cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    cancel.style.cssText = 'background:var(--surface); color:var(--ink); border:1px solid var(--border);' +
      'border-radius:9px; padding:8px 14px; font-size:12px; font-weight:600; cursor:pointer;';
    var save = document.createElement('button');
    save.textContent = 'Save';
    save.style.cssText = 'background:var(--edge); color:#fff; border:none; border-radius:9px;' +
      'padding:8px 16px; font-size:12px; font-weight:700; cursor:pointer;';
    row.appendChild(cancel);
    row.appendChild(save);
    card.appendChild(row);

    function close() { wrap.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    cancel.onclick = close;
    wrap.onclick = function (e) { if (e.target === wrap) close(); };
    document.addEventListener('keydown', onKey);
    save.onclick = function () {
      SI.configure({
        baseUrl: urlInput.value.trim() || window.location.origin,
        token: tokenInput.value
      });
      close();
      if (onSaved) onSaved();
    };

    wrap.appendChild(card);
    document.body.appendChild(wrap);
    urlInput.focus();
  }

  // ------------------------------------------------------------------ overrides
  Object.assign(P, {

    /* Append to the combined stream, and -- when the line belongs to a
     * specific target -- to that target's own stream too.
     *
     * Both, not either: the combined view is still the right place to watch a
     * fan-out land across twelve machines, while the per-device view is the
     * only way to read one machine's output without the other eleven
     * interleaved through it. */
    sshLog: function (lines, deviceId) {
      this.setState(function (s) {
        var ssh = Object.assign({}, s.ssh, { out: appendCollapsed(s.ssh.out, lines) });
        if (deviceId) {
          var by = Object.assign({}, ssh.outBy || {});
          by[deviceId] = appendCollapsed(by[deviceId] || [], lines);
          ssh.outBy = by;
        }
        return { ssh: ssh };
      });
    },

    // --- lifecycle -------------------------------------------------------
    componentDidMount: function () {
      base.componentDidMount.call(this);

      // After the base call, which installs `defaultStages()` -- this replaces
      // it with the previous session's inventory when there is one.
      var saved = loadSession();
      if (saved) {
        var knownIds = [];
        (saved.stages || []).forEach(function (s) {
          (s.devices || []).forEach(function (d) { knownIds.push(d.id); });
        });
        var patch = {
          stages: saved.stages,
          config: Object.assign({}, this.state.config, saved.config || {}),
          clusterCfg: saved.clusterCfg || {},
          ssh: Object.assign({}, this.state.ssh, {
            conn: (saved.ssh && saved.ssh.conn) || {},
            cwd: (saved.ssh && saved.ssh.cwd) || '',
            command: (saved.ssh && saved.ssh.command) || this.state.ssh.command,
            // Selection is filtered to devices that still exist, so an id left
            // over from a removed device cannot make "Run" target a ghost.
            selected: ((saved.ssh && saved.ssh.selected) || []).filter(function (id) {
              return id === SERVER_ID || knownIds.indexOf(id) >= 0;
            }),
            focus: (saved.ssh && saved.ssh.focus) || '',
            pullPath: (saved.ssh && saved.ssh.pullPath) || '',
            server: Object.assign({}, this.state.ssh.server, (saved.ssh && saved.ssh.server) || {})
          })
        };
        if (saved.uploadedModel) patch.uploadedModel = saved.uploadedModel;
        patch.viz = Object.assign(
          { dir: '', caseName: '', windowOn: false }, saved.viz || {}
        );
        this.setState(patch);
      }

      // The saved-report row is the only part of the panel that is useful
      // before anything has been analysed, so it loads whether or not the
      // panel is open.
      this.vizLoadSaved();

      // The hosted page knows its own origin, and injects a token only for
      // loopback callers (see app/routers/web.py). A token typed into the
      // settings dialog is never overwritten by a blank injection.
      var boot = window.__SPLIT_INFERENCE_BOOTSTRAP || {};
      var cfg = {};
      if (boot.baseUrl != null) cfg.baseUrl = boot.baseUrl || window.location.origin;
      if (boot.token) cfg.token = boot.token;
      if (Object.keys(cfg).length) SI.configure(cfg);

      // The card's port/user/password now mean SSH, so 5672 (the old AMQP
      // default baked into `state`) would be wrong on first paint.
      var sv = this.state.ssh.server;
      this.sshServerPatch({
        port: sv.port === 5672 || !sv.port ? 22 : sv.port,
        amqpPort: sv.amqpPort == null ? 5672 : sv.amqpPort,
        amqpUser: sv.amqpUser == null ? 'guest' : sv.amqpUser,
        user: sv.user === 'admin' ? '' : sv.user
      });

      /* Ctrl/Cmd+C and Ctrl/Cmd+V on the open device form.
       *
       * Only when the keystroke is not already doing its ordinary job:
       * inside a text field, or with text selected, the browser's own
       * copy/paste is what the operator meant. */
      this._siKeys = function (e) {
        if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
        var key = (e.key || '').toLowerCase();
        if (key !== 'c' && key !== 'v') return;

        var editing = self.state.ssh.editing;
        if (!editing || editing === SERVER_ID) return;

        var tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        var selection = window.getSelection && window.getSelection();
        if (key === 'c' && selection && !selection.isCollapsed) return;

        e.preventDefault();
        if (key === 'c') self.siCopyDevice(editing);
        else self.siPasteDevice(editing);
      };
      document.addEventListener('keydown', this._siKeys);

      /* ← → to surf the compared charts, Esc back to the full stack.
       *
       * Only on the Visual tab and only in compare mode: arrow keys belong to
       * whatever is focused everywhere else, and a form field keeps them even
       * here. */
      this._siVizKeys = function (e) {
        if (self.state.active !== 'visual') return;
        var viz = self.state.viz || {};
        if (!viz.compareOn) return;
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        var tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

        if (e.key === 'ArrowRight') { e.preventDefault(); self.vizCompareStep(1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); self.vizCompareStep(-1); }
        else if (e.key === 'Escape' && viz.cmpFocus) {
          e.preventDefault();
          self.vizPatch({ cmpFocus: '' });
        }
      };
      document.addEventListener('keydown', this._siVizKeys);

      this._siFx = installPointerFx();

      this.setState({ liveMode: SI.isLive() });

      var self = this;
      this.siPing().then(function (health) {
        // The stream carries `exec_line` -- the *only* channel command output
        // arrives on. The Control tab talks to real machines regardless of the
        // Live/Simulate toggle, so gating the stream on that toggle meant
        // commands ran on the server and their output went nowhere.
        if (!health) return;
        self.siOpenStream();
        self.siLoadPresets();
        self.siLoadDeviceConns().then(function (restored) {
          if (restored) {
            self.sshLog([{
              text: '· restored saved connection details for ' + restored + ' device(s)',
              color: C.hint
            }]);
          }
        });
      });

      if (SI.isLive()) this.siStartLive({ quiet: true });
      else this.siLoadServerConfig();
    },

    componentDidUpdate: function () {
      base.componentDidUpdate.call(this);
      followConsoleTail(this.state.ssh.focus || '');

      // Debounced: this fires on every keystroke and every console line, and
      // serialising the whole inventory that often is pure waste.
      var self = this;
      if (this._siSaveTimer) clearTimeout(this._siSaveTimer);
      this._siSaveTimer = setTimeout(function () { saveSession(self.state); }, 400);
    },

    componentWillUnmount: function () {
      base.componentWillUnmount.call(this);
      if (this._siStream) { this._siStream.close(); this._siStream = null; }
      if (this._siKeys) { document.removeEventListener('keydown', this._siKeys); this._siKeys = null; }
      if (this._siVizKeys) {
        document.removeEventListener('keydown', this._siVizKeys);
        this._siVizKeys = null;
      }
      if (this._siFx) { this._siFx(); this._siFx = null; }
      this.vizDropImages();
      this.vizDropCompareImages();
      if (this._siSaveTimer) { clearTimeout(this._siSaveTimer); this._siSaveTimer = null; }
      saveSession(this.state);   // flush whatever the debounce still holds
    },

    // --- connection state ------------------------------------------------
    /** One-shot reachability check; drives the header chip in either mode. */
    siPing: function () {
      var self = this;
      return SI.api.health().then(function (h) {
        self.setState({ siHealth: h, siReachable: true });
        return h;
      }).catch(function () {
        self.setState({ siHealth: null, siReachable: false });
        return null;
      });
    },

    siOpenStream: function () {
      if (this._siStream) return;
      var self = this;
      this._siStream = SI.openStream({
        onOpen: function () { self.setState({ siStreamUp: true, siReachable: true }); },
        onClose: function () { self.setState({ siStreamUp: false }); },
        onSshStatus: function (id, status) { self.sshSetStatus([id], status); },
        onServerStatus: function (status) { self.sshServerPatch({ status: status }); },
        onExecLine: function (id, text, stream) {
          // `id` is "" for meta lines that belong to the run as a whole
          // (`$ cmd → N host(s)`), which have no single owner.
          self.sshLog([{ text: text, color: lineColor(text, stream) }], id);
        },
        onMetrics: function (payload) { self.siApplyMetrics(payload); },
        onEvent: function (frame) {
          if (!frame || !frame.name) return;
          self.sshLog([{ text: '· ' + frame.name +
            (frame.cluster ? ' (cluster ' + frame.cluster + ')' : ''), color: C.hint }]);
        }
      });
    },

    siCloseStream: function () {
      if (this._siStream) { this._siStream.close(); this._siStream = null; }
      this.setState({ siStreamUp: false });
    },

    /* `toSimShape` returns null for an idle cluster, which is exactly what
     * `simCluster()` returns for one -- so a null is a real answer, not a
     * missing one, and `siSeen` is what distinguishes the two. `siRaw` keeps
     * the untranslated payload for the fields the sim shape has no room for
     * (queue_depth, per-device util, source). */
    siApplyMetrics: function (payload) {
      if (!payload || payload.cluster == null) return;
      var id = payload.cluster;
      var shaped = SI.toSimShape(payload);
      this.setState(function (s) {
        var metrics = Object.assign({}, s.siMetrics || {});
        var seen = Object.assign({}, s.siSeen || {});
        var raw = Object.assign({}, s.siRaw || {});
        metrics[id] = shaped;
        seen[id] = true;
        raw[id] = payload;
        return { siMetrics: metrics, siSeen: seen, siRaw: raw };
      });
    },

    // --- mode ------------------------------------------------------------
    siToggleMode: function () {
      if (SI.isLive()) this.siStopLive();
      else this.siStartLive({ quiet: false });
    },

    siStartLive: function (opts) {
      var self = this;
      var quiet = opts && opts.quiet;
      SI.configure({ mode: 'live' });
      this.setState({ liveMode: true });
      if (!quiet) this.sshLog([{ text: '⇄ live mode — syncing inventory to ' + SI.config.baseUrl, color: C.info }]);

      return this.siSyncInventory()
        .then(function () { return self.siLoadServerConfig(); })
        .then(function () { return SI.api.metricsLatest(); })
        .then(function (body) {
          (body.clusters || []).forEach(function (p) { self.siApplyMetrics(p); });
          self.setState({ ran: true, siReachable: true });
          self.siOpenStream();
          if (!quiet) self.sshLog([{ text: '✓ live — ' + (body.clusters || []).length +
            ' cluster(s), Σ ' + (body.aggregate_fps || 0).toFixed(1) + ' fps', color: C.ok }]);
        })
        .catch(function (e) {
          self.setState({ siReachable: false });
          self.sshLog([{ text: '✗ live mode: ' + errText(e), color: C.err }]);
          self.sshLog([{ text: '  check the backend URL / token — click the header chip', color: C.hint }]);
        });
    },

    siStopLive: function () {
      SI.configure({ mode: 'simulate' });
      this.siCloseStream();
      this.setState({ liveMode: false, siMetrics: {}, siSeen: {}, siRaw: {} });
      this.sshLog([{ text: '⇄ simulate mode — using in-browser math', color: C.info }]);
    },

    /* Push the UI's own export shape. `replace` stays false so that devices
     * already carrying host/username/password on the server keep them: a
     * replacing seed deletes the rows and forgets their secrets. Devices the
     * operator removed in the UI are pruned explicitly instead. */
    siSyncInventory: function () {
      var body = SI.seedFromUiState(this.state);
      body.replace = false;
      var keep = {};
      this.flatDevices().forEach(function (d) { keep[d.id] = true; });

      return SI.api.seed(body).then(function () {
        return SI.api.listDevices().then(function (rows) {
          var stale = (rows || []).filter(function (r) { return !keep[r.id]; });
          return Promise.all(stale.map(function (r) { return SI.api.deleteDevice(r.id); }));
        });
      });
    },

    siLoadServerConfig: function () {
      var self = this;
      return SI.api.getServerConfig().then(function (cfg) {
        if (!cfg) return cfg;
        var sv = self.state.ssh.server;
        self.sshServerPatch({
          ip: cfg.ip || sv.ip,
          // The card's main four fields are the SSH login.
          port: cfg.ssh_port || 22,
          user: cfg.ssh_user || cfg.ssh_username || sv.user,
          password: '',
          amqpHost: cfg.amqp_host || '',
          amqpHostResolved: cfg.amqp_host_resolved || '',
          amqpPort: cfg.port || 5672,
          amqpUser: cfg.user || 'guest',
          amqpPassword: '',
          jump: !!cfg.jump_enabled,
          status: cfg.status || 'off'
        });
        return cfg;
      }).catch(function () { return null; });
    },

    // --- metrics: one accessor for both modes ---------------------------
    simCluster: function (cl) {
      if (!SI.isLive()) return base.simCluster.call(this, cl);
      if ((this.state.siSeen || {})[cl.id]) return (this.state.siMetrics || {})[cl.id] || null;
      return base.simCluster.call(this, cl);   // no frame for it yet
    },

    // --- run buttons -----------------------------------------------------
    runSim: function () {
      if (!SI.isLive()) return base.runSim.call(this);
      var self = this;
      this.setState({ active: 'simulation' });
      return this.siSyncInventory()
        .then(function () { return SI.api.metricsLatest(); })
        .then(function (body) {
          var log = ['▶ live refresh · ' + new Date().toLocaleTimeString() +
            ' · ' + SI.config.baseUrl];
          (body.clusters || []).forEach(function (p) {
            self.siApplyMetrics(p);
            if (p.idle) { log.push('cluster ' + p.cluster + ': idle (' + (p.reason || 'no devices') + ')'); return; }
            log.push('cluster ' + p.cluster + ': cut@' + p.cut + '/' + p.layer_count +
              ' · msg ' + self.fmtMsg(p.msg_mb) + ' · ' + self.f1(p.fps) + ' fps · ' +
              (p.source === 'live' ? 'measured' : 'simulated server-side') +
              (p.queue_depth ? ' · queue ' + p.queue_depth : ''));
          });
          log.push('Σ aggregate throughput = ' + self.f1(body.aggregate_fps || 0) + ' fps');
          self.setState({ ran: true, flowing: true, log: log, siReachable: true });
        })
        .catch(function (e) {
          self.setState({ ran: true, log: ['✗ ' + errText(e)] });
        });
    },

    runFlow: function () {
      if (!SI.isLive()) return base.runFlow.call(this);
      var self = this;
      if (this.state.flowing) {
        this.sshLog([{ text: '■ stopping run…', color: C.info }]);
        return SI.api.stop(null)
          .then(function () {
            self.setState({ flowing: false });
            // The run just finished, which is the moment the results exist.
            // The status waits on the Visual tab rather than switching to it --
            // yanking the operator off a console they are still reading is a
            // worse trade than one extra click. The case name defaults to the
            // cut it ran at, since that is what distinguishes one result
            // directory from the next.
            var viz = self.state.viz || {};
            self.vizPatch({
              caseName: viz.caseName || self.siRunLabel(),
              status: 'run finished — pick the directory it wrote',
              statusKind: 'info'
            });
          })
          .catch(function (e) { self.sshLog([{ text: '✗ stop: ' + errText(e), color: C.err }]); });
      }
      this.setState({ active: 'pipeline' });
      this.sshLog([{ text: '▶ starting split inference on every runnable cluster…', color: C.info }]);
      return SI.api.start(null)
        .then(function (r) {
          self.setState({ flowing: true, ran: true });
          (r.started || []).forEach(function (run) {
            self.sshLog([{ text: '✓ cluster ' + run.cluster + ' running (cut@' + run.cut + ', run ' + run.run_id + ')', color: C.ok }]);
          });
        })
        .catch(function (e) {
          self.sshLog([{ text: '✗ run/start: ' + errText(e), color: C.err }]);
          if (e && e.status === 503) self.sshLog([{ text: '  RabbitMQ unreachable — check the broker card', color: C.hint }]);
          else self.sshLog([{ text: '  deploy the shards first (⇧ Deploy)', color: C.hint }]);
        });
    },

    // --- command presets --------------------------------------------------
    /* Repopulate the per-device ⚙ forms from what the backend already has.
     *
     * Host, port and username are stored the moment you connect, but the form
     * lives in browser state -- so after a reload it showed the placeholder
     * `root@10.0.1.x` again and looked like nothing had been saved. The
     * password deliberately does not come back (it is write-only), so a stored
     * one is shown as a marker instead: `has_password` tells us it is on file,
     * and sending the marker back unchanged would overwrite the real one, so
     * `siConnectDevice` strips it. */
    siLoadDeviceConns: function () {
      var self = this;
      return SI.api.listDevices().then(function (rows) {
        var conn = Object.assign({}, self.state.ssh.conn || {});
        var restored = 0;
        (rows || []).forEach(function (r) {
          if (!r.host) return;   // never configured; leave the placeholder
          var local = conn[r.id];
          if (local && local.ip) {
            // Locally saved details win -- they may be mid-edit. But the
            // password is never in local storage, so an empty box next to a
            // stored password reads as "you have to type this again".
            if (!local.password && r.has_password) {
              conn[r.id] = Object.assign({}, local, { password: STORED_PASSWORD });
              restored += 1;
            }
            return;
          }
          conn[r.id] = {
            ip: r.host,
            port: r.port || 22,
            user: r.username || 'root',
            password: r.has_password ? STORED_PASSWORD : ''
          };
          restored += 1;
        });
        if (restored) self.sshPatch({ conn: conn });
        return restored;
      }).catch(function () { return 0; });
    },

    siLoadPresets: function () {
      var self = this;
      return Promise.all([
        SI.api.listPresets().catch(function () { return null; }),
        SI.api.listDirectories().catch(function () { return null; })
      ]).then(function (rs) {
        var patch = {};
        if (rs[0]) patch.siPresets = rs[0].presets || [];
        if (rs[1]) patch.siDirs = rs[1].directories || [];
        if (Object.keys(patch).length) self.setState(patch);
        return rs;
      });
    },

    siEditPresets: function () {
      var self = this;
      var current = this.state.siPresets;
      if (!current || !current.length) {
        // Falls back to whatever the page is currently showing, so the editor
        // is never empty just because the fetch has not landed.
        current = (this.renderVals().ssh.presets || []).map(function (p) {
          return { label: p.label, command: p.label };
        });
      }
      openPresetEditor(
        current,
        this.state.siDirs || [],
        function (presets, dirs) {
          Promise.all([SI.api.savePresets(presets), SI.api.saveDirectories(dirs)])
            .then(function (rs) {
              self.setState({
                siPresets: rs[0].presets || [],
                siDirs: rs[1].directories || []
              });
              self.sshLog([{
                text: '✓ saved ' + (rs[0].presets || []).length + ' command(s), ' +
                  (rs[1].directories || []).length + ' directory(ies)',
                color: C.ok
              }]);
            })
            .catch(function (e) { self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]); });
        },
        function () {
          SI.api.resetPresets()
            .then(function (r) {
              self.setState({ siPresets: r.presets || [] });
              self.sshLog([{ text: '· commands restored to defaults', color: C.hint }]);
            })
            .catch(function (e) { self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]); });
        }
      );
    },

    siDeploy: function () {
      var self = this;
      this.setState({ active: 'control' });
      this.sshLog([{ text: '⇧ deploying shards + agents to every cluster…', color: C.info }]);
      return SI.api.deploy(null, { install_deps: false })
        .then(function (r) {
          ((r && r.clusters) || []).forEach(function (c) {
            var n = (c.edge_devices || []).length + (c.cloud_devices || []).length;
            self.sshLog([{
              text: (c.ok ? '✓' : '✗') + ' cluster ' + c.cluster + ' → ' + c.queue +
                ': ' + n + ' device(s), ' + (c.transfers || []).length + ' transfer(s)',
              color: c.ok ? C.ok : C.err
            }]);
          });
          self.sshLog([{ text: '✓ deploy complete — press "Run & animate flow" to start', color: C.ok }]);
        })
        .catch(function (e) { self.sshLog([{ text: '✗ deploy: ' + errText(e), color: C.err }]); });
    },

    // --- control tab -----------------------------------------------------
    /* Copy one device's login so the next one does not have to be retyped.
     *
     * The IP is deliberately **not** part of it: two machines cannot share an
     * address, so pasting it is never what was wanted, and silently
     * duplicating it would produce a device that connects to the wrong host.
     * Port, username and password are the parts that genuinely repeat across a
     * fleet.
     *
     * Kept in component state rather than the system clipboard: a password on
     * the OS clipboard outlives this page and is readable by anything else
     * running on the machine. */
    siCopyDevice: function (id) {
      var conn = (this.state.ssh.conn || {})[id] || {};
      var device = this.flatDevices().filter(function (d) { return d.id === id; })[0];
      var name = device ? device.name : id;
      if (!conn.user && !conn.password) {
        this.sshLog([{ text: '✗ nothing to copy from ' + name + ' — fill the form first',
          color: C.err }]);
        return;
      }
      this.sshPatch({
        clip: {
          from: name,
          port: conn.port || 22,
          user: conn.user || '',
          password: conn.password || ''
        }
      });
      this.sshLog([{
        text: '⧉ copied login from ' + name + ' (port, username' +
          (conn.password ? ', password' : '') + ') — paste into another device with Ctrl+V',
        color: C.info
      }]);
    },

    siPasteDevice: function (id) {
      var clip = this.state.ssh.clip;
      if (!clip) {
        this.sshLog([{ text: '✗ nothing copied yet', color: C.err }]);
        return;
      }
      var device = this.flatDevices().filter(function (d) { return d.id === id; })[0];
      this.sshConnPatch(id, {
        port: clip.port, user: clip.user, password: clip.password
      });
      this.sshLog([{
        text: '⧉ pasted ' + clip.from + "'s login into " +
          (device ? device.name : id) + ' — IP left as it was',
        color: C.ok
      }]);
    },

    /* Tick or clear one stage's targets.
     *
     * The list is grouped by stage and a fan-out command is aimed at a stage --
     * every edge box, or just the ones in stage 2 -- so the toggle belongs to
     * the group rather than to the panel. It clears only when every row in the
     * group is already ticked, so the button never quietly drops a selection
     * that was made row by row. Targets in other stages are left alone either
     * way: selecting a stage is additive.
     *
     * `preset` is the stage's own run command, resolved at render time (see
     * `groupSelect` in renderVals) and loaded into the command box on the way
     * in. Picking the stage and saying what to run on it is one decision --
     * "run stage 1" is only ever aimed at stage 1's boxes -- and typing it
     * again for a list you have just pointed at is the step this removes. Only
     * on the way in: clearing a stage says nothing about what to run next, so
     * the box keeps whatever is in it. */
    siSelectGroup: function (ids, preset) {
      // Read before the update, so the log line and the command agree about
      // which way the click went.
      var before = this.state.ssh.selected || [];
      var selecting = !ids.every(function (id) { return before.indexOf(id) >= 0; });

      this.setState(function (s) {
        var sel = s.ssh.selected || [];
        var picked = function (id) { return sel.indexOf(id) >= 0; };
        var next = ids.every(picked)
          ? sel.filter(function (id) { return ids.indexOf(id) < 0; })
          : sel.concat(ids.filter(function (id) { return !picked(id); }));
        var ssh = Object.assign({}, s.ssh, { selected: next });
        if (selecting && preset) ssh.command = preset.command;
        return { ssh: ssh };
      });

      // Said out loud: the command box may be scrolled out of view, and a
      // command that changed itself is worth noticing before Run is pressed.
      if (selecting && preset) {
        this.sshLog([{
          text: '· command set to “' + preset.label + '” — ' + preset.command,
          color: C.hint
        }]);
      }
    },

    /** Open one device's session, from its own ⚙ form. */
    siConnectDevice: function (id) {
      if (!controlLive(this)) {
        mockNotice(this, 'connecting');
        return base.sshConnectAll.call(this, true);
      }
      var self = this;
      var conn = (this.state.ssh.conn || {})[id] || {};
      if (!conn.ip) {
        this.sshLog([{ text: '✗ fill in IP / host first', color: C.err }]);
        return;
      }

      this.sshSetStatus([id], 'connecting');
      return this.siSyncInventory()
        .then(function () { return SI.api.connect([id], realCreds(self.state.ssh.conn)); })
        .then(function (r) { self.siApplyStatuses(r); })
        .catch(function (e) {
          self.sshSetStatus([id], 'error');
          self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
        });
    },

    // --- measuring device specs -----------------------------------------
    /* Measure every device in one stage and replace its specs with the result.
     *
     * A stage rather than a device is the unit because bandwidth is: the
     * backend measures each machine alone (so they do not read each other's
     * share of the uplink) and then all of them together, and the ratio
     * between those two only means anything for a set of machines that
     * actually share a link. A stage is the closest thing the UI has to that
     * set -- the edges sit behind one uplink, the cloud nodes behind another. */
    siMeasureStage: function (stageId) {
      var self = this;
      var stage = (this.state.stages || []).filter(function (s) {
        return s.id === stageId;
      })[0];
      if (!stage) return;

      var ids = (stage.devices || []).map(function (d) { return d.id; });
      if (!ids.length) {
        this.sshLog([{ text: '✗ ' + stage.name + ' has no devices to measure', color: C.err }]);
        return;
      }
      if (!controlLive(this)) {
        // Nothing to fall back to: there is no simulated hardware to time.
        mockNotice(this, 'measurement');
        return;
      }
      if (this.siAnyMeasuring()) return;   // this stage, or a fleet pass

      this.siSetMeasuring(stageId, true);
      this.sshLog([{
        text: '⇄ measuring ' + ids.length + ' device(s) in ' + stage.name +
          ' — compute and latency together, bandwidth one at a time',
        color: C.info
      }]);
      if (!SI.isLive()) {
        this.sshLog([{
          text: '  switch to Live to watch it progress; results land here either way',
          color: C.hint
        }]);
      }

      // Sync first, exactly as connecting does: a device added since the last
      // sync has no row on the server, and measuring it would 404.
      return this.siSyncInventory()
        .then(function () {
          return SI.api.measureFleet({
            device_ids: ids,
            apply: true,
            contention: true
          });
        })
        .then(function (r) { self.siApplyMeasured(stageId, r); })
        .catch(function (e) {
          self.sshLog([{ text: '✗ measure: ' + errText(e), color: C.err }]);
        })
        .then(function () { self.siSetMeasuring(stageId, false); });
    },

    /* Measure every device in every stage in one pass.
     *
     * Not the same as pressing ⟳ measure on each stage in turn, and the
     * difference is the whole point. The contended figure is measured with
     * everything in the request publishing at once, so a per-stage pass has
     * the 9 edges contending with each other and the 3 clouds contending with
     * each other -- while a real run has all twelve pushing into the same
     * broker together. Only a fleet-wide pass reproduces that, which makes it
     * the one whose `bandwidth_mb_s` the simulator should be fed. */
    siMeasureAll: function () {
      var self = this;
      var stages = (this.state.stages || []).filter(function (s) {
        return (s.devices || []).length;
      });
      var ids = [];
      stages.forEach(function (stage) {
        (stage.devices || []).forEach(function (d) { ids.push(d.id); });
      });
      if (!ids.length) {
        this.sshLog([{ text: '✗ no devices to measure', color: C.err }]);
        return;
      }
      if (!controlLive(this)) {
        mockNotice(this, 'measurement');
        return;
      }
      if (this.siAnyMeasuring()) return;

      stages.forEach(function (stage) { self.siSetMeasuring(stage.id, true); });
      this.sshLog([{
        text: '⇄ measuring all ' + ids.length + ' device(s) across ' + stages.length +
          ' stage(s) — bandwidth is taken with every device publishing at once, ' +
          'which is what a run does',
        color: C.info
      }]);

      return this.siSyncInventory()
        .then(function () {
          return SI.api.measureFleet({
            device_ids: ids, apply: true, contention: true
          });
        })
        .then(function (r) { self.siApplyMeasured(null, r); })
        .catch(function (e) {
          self.sshLog([{ text: '✗ measure: ' + errText(e), color: C.err }]);
        })
        .then(function () {
          stages.forEach(function (stage) { self.siSetMeasuring(stage.id, false); });
        });
    },

    siAnyMeasuring: function () {
      var busy = this.state.siMeasuring || {};
      return Object.keys(busy).some(function (k) { return busy[k]; });
    },

    siSetMeasuring: function (stageId, on) {
      this.setState(function (s) {
        var next = Object.assign({}, s.siMeasuring || {});
        if (on) next[stageId] = true; else delete next[stageId];
        return { siMeasuring: next };
      });
    },

    /* Write measured specs onto the device cards.
     *
     * `null` for a field means "could not be measured" and is left alone, not
     * written as zero: a device where torch is missing should keep the GFLOPS
     * someone typed rather than have the card blanked by a failed probe. The
     * server applies the same rule to its own row. */
    /* deviceId -> the stage it sits in. */
    siStageIndex: function () {
      var index = {};
      (this.state.stages || []).forEach(function (stage) {
        (stage.devices || []).forEach(function (d) { index[d.id] = stage.id; });
      });
      return index;
    },

    /* `stageId` may be null, meaning "each device belongs to whichever stage
     * it is in" -- that is the fleet-wide measure, whose results span stages
     * and whose notice therefore has to be written to each of them. */
    siApplyMeasured: function (stageId, response) {
      var self = this;
      var index = stageId ? null : this.siStageIndex();
      var perStage = {};
      function bucket(id) {
        if (!perStage[id]) {
          perStage[id] = { ok: [], failed: [], fields: { gflops: 0, bw: 0, lat: 0 }, total: 0 };
        }
        return perStage[id];
      }
      var results = (response && response.results) || [];
      if (!results.length) {
        this.sshLog([{ text: '· nothing was measured', color: C.hint }]);
        return;
      }

      var applied = 0;

      results.forEach(function (m) {
        var label = m.device_name || m.device_id;
        var sid = stageId || (index || {})[m.device_id];
        if (!sid) return;                       // a device no stage claims
        var stats = bucket(sid);
        stats.total += 1;
        var fields = stats.fields;

        if (!m.ok) {
          stats.failed.push(label);
          self.sshLog([{
            text: '✗ ' + label + ': ' + (m.error || 'unreachable'), color: C.err
          }], m.device_id);
          return;
        }

        var patch = {};
        var shown = [];
        if (m.gflops != null) { patch.gflops = spec(m.gflops, 1); shown.push(patch.gflops + ' GFLOPS'); fields.gflops += 1; }
        if (m.bandwidth_mb_s != null) { patch.bw = spec(m.bandwidth_mb_s, 1); shown.push(patch.bw + ' MB/s'); fields.bw += 1; }
        if (m.latency_ms != null) { patch.lat = spec(m.latency_ms, 2); shown.push(patch.lat + ' ms'); fields.lat += 1; }

        if (!shown.length) {
          // Reachable is not measured. Counting it as a success would put a
          // tick next to a card whose numbers nothing has touched.
          stats.failed.push(label);
          self.sshLog([{
            text: '· ' + label + ': reachable, but nothing could be measured', color: C.hint
          }], m.device_id);
        } else {
          self.updateDevice(sid, m.device_id, patch);
          applied += 1;
          stats.ok.push(label);
          self.sshLog([{
            text: '✓ ' + label + '  ' + shown.join('  ·  ') +
              '   (' + sourceList(m.sources) + ')',
            color: C.ok
          }], m.device_id);
        }

        // Warnings say which numbers are estimates rather than measurements,
        // which is the difference between a spec you can trust and one you
        // should re-take with iperf3 running. Never swallowed.
        (m.warnings || []).forEach(function (w) {
          self.sshLog([{ text: '  ⚠ ' + label + ': ' + w, color: C.warn }], m.device_id);
        });
      });

      var summary = (response && response.summary) || {};
      if (summary.contention_pass) {
        var worst = summary.worst_contention_ratio;
        this.sshLog([{
          text: '· under load the busiest device keeps ' +
            (worst == null ? '?' : Math.round(worst * 100) + '%') +
            ' of its solo bandwidth; ' + Math.round(summary.aggregate_shared_mb_s || 0) +
            ' MB/s across the group',
          color: C.body
        }]);
      } else if (summary.contention_skipped) {
        this.sshLog([{ text: '· no contention pass: ' + summary.contention_skipped, color: C.hint }]);
      }
      this.sshLog([{
        text: '⇄ ' + applied + '/' + results.length + ' device(s) updated',
        color: applied ? C.ok : C.warn
      }]);

      // Reported on the stage card as well as in the console: whoever clicked
      // ⟳ measure is looking at the cards, not at the Control tab, and a
      // partial result has to be visible from there.
      Object.keys(perStage).forEach(function (id) {
        var stats = perStage[id];
        self.siSetNotice(id, {
          ok: stats.ok, failed: stats.failed, fields: stats.fields,
          total: stats.total, at: Date.now()
        });
      });
    },

    /* The stage's notice line, from the last measurement's outcome.
     *
     * A method rather than a closure so it is reachable the way every other
     * handler here is -- `renderVals` is far too heavy to drive in a test, and
     * this is the piece whose wording and colour actually need pinning down. */
    siNoticeFor: function (notice, busy) {
      if (busy) {
        return {
          hasNotice: true,
          noticeText: 'measuring… compute and latency together, bandwidth one device at a time',
          noticeStyle: noticeStyle('var(--broker)')
        };
      }
      if (!notice) return { hasNotice: false, noticeText: '', noticeStyle: {} };

      var ok = notice.ok || [];
      var failed = notice.failed || [];
      var fields = notice.fields || {};
      var text, color;

      if (!ok.length) {
        return {
          hasNotice: true,
          noticeText: '✗ none of ' + notice.total + ' measured: ' + nameList(failed),
          noticeStyle: noticeStyle('var(--alert)')
        };
      }

      // Name the fields that landed on every device that answered, and the
      // ones that did not land at all -- those are the cards still showing
      // whatever was typed there before.
      var got = [];
      var missing = [];
      [['gflops', 'GFLOPS'], ['bw', 'MB/s'], ['lat', 'LAT MS']].forEach(function (f) {
        var n = fields[f[0]] || 0;
        if (n >= ok.length) got.push(f[1]);
        else if (n === 0) missing.push(f[1]);
        else got.push(f[1] + ' (' + n + '/' + ok.length + ')');
      });

      text = '✓ measured ' + ok.length + '/' + notice.total + ' · ' +
        (got.length ? got.join(', ') : 'nothing');
      if (missing.length) text += ' · ' + missing.join(' + ') + ' not measured';
      if (failed.length) text += ' · skipped: ' + nameList(failed);

      color = (missing.length || failed.length) ? 'var(--server)' : 'var(--data)';
      return { hasNotice: true, noticeText: text, noticeStyle: noticeStyle(color) };
    },

    // --- destructive: removing stages and devices ------------------------
    /* Both ✕ buttons delete without asking in the stock UI, and both delete
     * more than they appear to.
     *
     * A device is not only a card: `siSyncInventory` prunes anything the UI no
     * longer lists, and `DELETE /devices/{id}` forgets that device's stored
     * SSH password along with the specs just measured for it. A stage takes
     * every device inside it the same way. None of that is recoverable from
     * the UI, and the button that does it sits a few pixels from ⟳ measure.
     *
     * `window.confirm` rather than a styled modal: it is what `sshRun` already
     * uses for destructive presets, and a second confirmation style for the
     * same class of action is worse than a plain one. */
    removeStage: function (id) {
      var stage = (this.state.stages || []).filter(function (s) {
        return s.id === id;
      })[0];
      if (!stage) return base.removeStage.call(this, id);

      var devices = stage.devices || [];
      var message = 'Remove the "' + stage.name + '" stage?';
      if (devices.length) {
        message += '\n\nIts ' + devices.length + ' device(s) go with it:\n  ' +
          devices.map(function (d) { return d.name; }).join(', ') +
          '\n\nTheir measured specs and any stored SSH passwords are deleted ' +
          'from the server on the next sync.';
      }
      if (!window.confirm(message)) return;

      // Drop the stage's own bookkeeping too, so a later stage cannot inherit
      // a stale "measured 9/9" notice if an id is ever reused.
      this.setState(function (s) {
        var measured = Object.assign({}, s.siMeasured || {});
        var measuring = Object.assign({}, s.siMeasuring || {});
        delete measured[id];
        delete measuring[id];
        return { siMeasured: measured, siMeasuring: measuring };
      });
      base.removeStage.call(this, id);
    },

    removeDevice: function (stageId, id) {
      var stage = (this.state.stages || []).filter(function (s) {
        return s.id === stageId;
      })[0];
      var device = ((stage || {}).devices || []).filter(function (d) {
        return d.id === id;
      })[0];
      if (!device) return base.removeDevice.call(this, stageId, id);

      var message = 'Remove "' + device.name + '"?' +
        '\n\nIts measured specs and any stored SSH password are deleted from ' +
        'the server on the next sync.';
      if (!window.confirm(message)) return;
      base.removeDevice.call(this, stageId, id);
    },

    siSetNotice: function (stageId, notice) {
      this.setState(function (s) {
        var next = Object.assign({}, s.siMeasured || {});
        next[stageId] = notice;
        return { siMeasured: next };
      });
    },

    /* Adopt the statuses the connect response reports.
     *
     * Waiting for an `ssh_status` frame is not enough: the pool suppresses a
     * status it has already published, so reconnecting a session that is
     * *still open* emits nothing at all -- leaving the optimistic
     * "connecting…" on screen for a device that is working fine. The response
     * always states where every requested device ended up. */
    siApplyStatuses: function (response) {
      if (!response) return;
      var statuses = response.statuses || {};
      var byId = {};
      Object.keys(response.results || {}).forEach(function (id) {
        byId[id] = (response.results[id] || {}).status;
      });
      var self = this;
      Object.keys(statuses).forEach(function (id) {
        if (byId[id] === undefined) byId[id] = statuses[id];
      });
      Object.keys(byId).forEach(function (id) {
        if (byId[id]) self.sshSetStatus([id], byId[id]);
      });
    },

    /* Connects the selected devices, or all of them when nothing is selected.
     * Dialling hosts you did not ask for produces a screenful of
     * `no host configured` from the placeholder devices and buries the one
     * result you were waiting on. */
    sshConnectAll: function (on) {
      if (!controlLive(this)) {
        if (on) mockNotice(this, 'connecting');
        return base.sshConnectAll.call(this, on);
      }
      var self = this;
      var selected = (this.state.ssh.selected || []).filter(function (id) {
        return id !== SERVER_ID;   // the server has its own Connect button
      });
      var ids = selected.length
        ? selected
        : this.flatDevices().map(function (d) { return d.id; });
      if (!ids.length) return;

      if (!on) {
        return SI.api.disconnect(ids)
          .then(function (r) {
            self.sshSetStatus(ids, 'off');
            self.siApplyStatuses(r);
          })
          .catch(function (e) {
            self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
          });
      }

      this.sshSetStatus(ids, 'connecting');
      // Per-device output arrives as exec_line frames; the final status comes
      // from the response, which reports every device including the ones the
      // pool had nothing new to say about.
      return this.siSyncInventory()
        .then(function () { return SI.api.connect(ids, realCreds(self.state.ssh.conn)); })
        .then(function (r) { self.siApplyStatuses(r); })
        .catch(function (e) {
          self.sshSetStatus(ids, 'error');
          self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
        });
    },

    sshRun: function (confirmed) {
      if (!controlLive(this)) {
        mockNotice(this, 'this command');
        return base.sshRun.call(this);
      }
      var self = this;
      var sel = this.state.ssh.selected;
      var cmd = (this.state.ssh.command || '').trim();
      if (!sel.length || !cmd) return;

      if (INTERRUPT_RE.test(cmd)) {
        this.sshPatch({ busy: true });
        return SI.api.stopExec(sel)
          .then(function (r) {
            if (!(r && (r.stopped || []).length)) {
              self.sshLog([{ text: '· nothing was running', color: C.hint }]);
            }
            // Outcomes stream back as exec_line frames, same as the output was.
          })
          .catch(function (e) { self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]); })
          .then(function () { self.sshPatch({ busy: false }); });
      }

      var cwd = (this.state.ssh.cwd || '').trim();
      this.sshPatch({ busy: true });
      return SI.api.exec(sel, cmd, !!confirmed, cwd)
        .catch(function (e) {
          if (e && e.status === 404 && /unknown device/i.test(errText(e))) {
            // The devices exist in the browser but were never pushed (the
            // operator went straight to Control without switching to Live).
            // Sync and retry once rather than making them guess.
            return self.siSyncInventory().then(function () {
              return SI.api.exec(sel, cmd, !!confirmed, cwd);
            });
          }
          throw e;
        })
        .catch(function (e) {
          if (e && e.status === 409) {
            // Destructive preset. The server decides what needs confirming;
            // the UI just relays its message rather than guessing.
            if (window.confirm(errText(e) + '\n\nRun it on ' + sel.length + ' host(s)?')) {
              return SI.api.exec(sel, cmd, true, cwd);
            }
            self.sshLog([{ text: '· cancelled', color: C.hint }]);
            return null;
          }
          self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
          return null;
        })
        .then(function () { self.sshPatch({ busy: false }); });
    },

    sshScp: function () {
      if (!controlLive(this)) {
        mockNotice(this, 'this transfer');
        return base.sshScp.call(this);
      }
      var self = this;
      var sel = this.state.ssh.selected;
      if (!sel.length) return;
      if (!this._siScpFile) {
        this.sshLog([{ text: '✗ choose a local file first', color: C.err }]);
        return;
      }
      this.sshPatch({ busy: true });
      return SI.api.scp(sel, this._siScpFile, this.state.ssh.scpRemote)
        .catch(function (e) { self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]); })
        .then(function () { self.sshPatch({ busy: false }); });
    },

    /* The card's primary action: save, then log in over SSH and run
     * `uname -a`. Nothing else. The broker is a separate concern that only
     * matters once split inference is running, and making it a precondition
     * for "can I get a shell" is how you end up with a red dot on a server you
     * can log into perfectly well. `sshServerTestAll` does the full check. */
    /* Pull and browse act on ONE device -- the first selected, or the control
     * server. Fanning a download across twelve machines would mean twelve
     * files of the same name racing for the same save dialog. */
    siPullTarget: function () {
      var selected = (this.state.ssh.selected || []);
      return selected.length ? selected[0] : SERVER_ID;
    },

    siBrowse: function () {
      var self = this;
      var id = this.siPullTarget();
      var path = (this.state.ssh.pullPath || '').trim() || '.';
      this.sshPatch({ browseBusy: true });
      return SI.api.remoteLs(id, path)
        .then(function (r) {
          self.sshPatch({ browseEntries: r.entries || [], browsePath: r.path, browseBusy: false });
          if (!(r.entries || []).length) {
            self.sshLog([{ text: '· ' + r.path + ' is empty', color: C.hint }], id);
          }
        })
        .catch(function (e) {
          self.sshPatch({ browseBusy: false, browseEntries: [] });
          self.sshLog([{ text: '✗ browse: ' + errText(e), color: C.err }], id);
        });
    },

    siPull: function () {
      var self = this;
      var id = this.siPullTarget();
      var path = (this.state.ssh.pullPath || '').trim();
      if (!path) {
        this.sshLog([{ text: '✗ enter a path to pull, or press browse', color: C.err }]);
        return;
      }
      this.sshLog([{ text: '⇩ pulling ' + path + ' …', color: C.info }], id);
      return SI.api.pullFile(id, path)
        .then(function (r) {
          self.sshLog([{ text: '✓ saved ' + r.name + ' (' + fmtBytes(r.bytes) + ') to your downloads',
            color: C.ok }], id);
        })
        .catch(function (e) {
          self.sshLog([{ text: '✗ pull: ' + errText(e), color: C.err }], id);
        });
    },

    /* ------------------------------------------------------------- visual
     *
     * "The run finished, now show me what happened." The Files card pulls one
     * file; this pulls the whole result directory, charts it server-side to
     * `guides/visual_guide.md`, and keeps the short note written against each
     * chart. Saved reports are folders on the backend, so a review outlives
     * this tab -- and the panel collapses once read, because a gallery of
     * charts parked above the console is in the way of the next command.
     */
    /* A default case-test name from what the run actually was. `yolov11n-cut6`
     * beats an empty box, and is what distinguishes one result directory from
     * the next when six of them are saved an hour apart. */
    siRunLabel: function () {
      var cfg = this.state.config || {};
      var parts = [];
      if (cfg.modelName) parts.push(String(cfg.modelName).replace(/\s+/g, '-'));
      if (cfg.manualEnabled && cfg.manualSplit != null) parts.push('cut' + cfg.manualSplit);
      else if (cfg.autoBalance) parts.push(cfg.autoBalance);
      return parts.join('-') || 'run';
    },

    vizPatch: function (patch) {
      this.setState(function (s) {
        return { viz: Object.assign({}, s.viz || {}, patch) };
      });
    },

    /* Object URLs are owned by this component: without the revoke the blobs
     * stay alive for the life of the tab, and re-analysing a few times leaks
     * every chart PNG that was ever shown.
     *
     * The compare slots are deliberately *not* dropped here: this runs whenever
     * a new single report is opened or analysed, and doing that while three
     * reports are pinned beside it would blank the comparison. They have their
     * own teardown, called when a slot goes and when the tab unmounts. */
    vizDropImages: function () {
      var srcs = (this.state.viz || {}).srcs || {};
      Object.keys(srcs).forEach(function (k) {
        try { URL.revokeObjectURL(srcs[k]); } catch (e) { /* already gone */ }
      });
    },

    vizDropCompareImages: function () {
      var sets = (this.state.viz || {}).cmpSrcs || {};
      Object.keys(sets).forEach(function (rid) {
        var set = sets[rid] || {};
        Object.keys(set).forEach(function (k) {
          try { URL.revokeObjectURL(set[k]); } catch (e) { /* already gone */ }
        });
      });
    },

    vizLoadImages: function (report) {
      var self = this;
      var charts = (report && report.charts) || [];
      // `rendered_at` busts the image cache. A chart PNG is served with a long
      // max-age, which was safe while a file name meant one fixed image; a
      // config change re-draws it in place under the same name.
      var version = (report && report.rendered_at) || '';
      return Promise.all(charts.map(function (c) {
        return SI.api.chartImage(report.id, c.file, version)
          .then(function (src) { return { id: c.id, src: src }; })
          .catch(function () { return { id: c.id, src: '' }; });
      })).then(function (pairs) {
        var srcs = {};
        pairs.forEach(function (p) { srcs[p.id] = p.src; });
        self.vizPatch({ srcs: srcs });
        return srcs;
      });
    },

    /* ---- per-chart config -------------------------------------------------
     *
     * The charts are PNGs drawn on the backend, so hiding a series or renaming
     * an axis is not a client-side toggle -- Apply sends the overrides and the
     * report is drawn again. Edits are held in `drafts` until then, so a
     * half-typed axis label does not trigger a re-render on every keystroke.
     */
    vizConfigOpen: function (id) {
      var viz = this.state.viz || {};
      // One panel at a time: two open at once is a lot of card above a chart
      // you are trying to read, and there is nothing to compare between them.
      var open = viz.configFor === id ? '' : id;
      var drafts = Object.assign({}, viz.drafts || {});
      if (open && !drafts[open]) {
        var chart = ((viz.report || {}).charts || []).filter(
          function (c) { return c.id === open; })[0];
        var stored = (chart && chart.view) || {};
        drafts[open] = {
          title: stored.title || '', xlabel: stored.xlabel || '',
          ylabel: stored.ylabel || '', hidden: (stored.hidden || []).slice()
        };
      }
      this.vizPatch({ configFor: open, drafts: drafts });
    },

    vizDraft: function (id, patch) {
      var viz = this.state.viz || {};
      var drafts = Object.assign({}, viz.drafts || {});
      drafts[id] = Object.assign({ title: '', xlabel: '', ylabel: '', hidden: [] },
        drafts[id] || {}, patch);
      this.vizPatch({ drafts: drafts });
    },

    vizToggleSeries: function (id, key) {
      var draft = (this.state.viz || {}).drafts || {};
      var hidden = ((draft[id] || {}).hidden || []).slice();
      var at = hidden.indexOf(key);
      if (at >= 0) hidden.splice(at, 1);
      else hidden.push(key);
      this.vizDraft(id, { hidden: hidden });
    },

    /* Every chart's overrides, stored ones plus the open draft. The whole map
     * goes over: it is small, and sending a diff would mean the server and the
     * panel could disagree about what is currently hidden. */
    vizViews: function () {
      var viz = this.state.viz || {};
      var views = {};
      function meaningful(v) {
        return !!(v && (v.title || v.xlabel || v.ylabel || (v.hidden || []).length));
      }
      ((viz.report || {}).charts || []).forEach(function (c) {
        if (meaningful(c.view)) views[c.id] = c.view;
      });
      Object.keys(viz.drafts || {}).forEach(function (id) {
        // An emptied draft is how Reset is expressed, so it *removes* an entry.
        if (meaningful(viz.drafts[id])) views[id] = viz.drafts[id];
        else delete views[id];
      });
      return views;
    },

    vizApplyViews: function (views) {
      var self = this;
      var viz = this.state.viz || {};
      var report = viz.report;
      if (!report) return;
      // The images on screen stay up while the new ones are drawn, and are
      // revoked only once they have been replaced -- blanking ten charts for
      // two seconds to change one of them reads as a failure.
      var stale = viz.srcs || {};
      this.vizPatch({ busy: true, status: 'redrawing charts…', statusKind: 'info' });

      return SI.api.saveReportViews(report.id, views || this.vizViews())
        .then(function (fresh) {
          var notes = {};
          (fresh.charts || []).forEach(function (c) { notes[c.id] = c.note || ''; });
          self.vizPatch({
            busy: false, report: fresh, notes: notes, drafts: {},
            status: 'redrew ' + (fresh.charts || []).length + ' chart(s)',
            statusKind: 'ok'
          });
          return self.vizLoadImages(fresh).then(function () {
            Object.keys(stale).forEach(function (k) {
              try { URL.revokeObjectURL(stale[k]); } catch (e) { /* already gone */ }
            });
          });
        })
        .catch(function (e) {
          self.vizPatch({ busy: false, status: errText(e), statusKind: 'err' });
        });
    },

    vizResetChart: function (id) {
      var views = this.vizViews();
      delete views[id];
      this.vizDraft(id, { title: '', xlabel: '', ylabel: '', hidden: [] });
      return this.vizApplyViews(views);
    },

    vizBrowse: function () {
      var self = this;
      var id = this.siPullTarget();
      var viz = this.state.viz || {};
      var path = (viz.dir || '').trim() || '.';
      this.vizPatch({ browseBusy: true });
      return SI.api.remoteLs(id, path)
        .then(function (r) {
          self.vizPatch({ browseEntries: r.entries || [], browseBusy: false });
        })
        .catch(function (e) {
          self.vizPatch({ browseBusy: false, browseEntries: [] });
          self.vizPatch({ status: 'browse: ' + errText(e), statusKind: 'err' });
        });
    },

    vizAnalyze: function () {
      var self = this;
      var viz = this.state.viz || {};
      var id = this.siPullTarget();
      var dir = (viz.dir || '').trim();
      if (!dir) {
        this.vizPatch({ status: 'pick the directory the run wrote, or press browse',
          statusKind: 'err' });
        return;
      }
      // A window that does not describe a slice of a run is caught before the
      // pull: the SSH round trip is the slow part, and it would only end in a
      // 422 about the same two numbers.
      var window_ = vizWindowOf(viz);
      if (window_ && window_.error) {
        this.vizPatch({ status: window_.error, statusKind: 'err' });
        return;
      }
      var scope = window_ ? ' (' + vizWindowLabel(window_) + ' of it)' : '';

      this.vizDropImages();
      this.vizPatch({ busy: true, status: 'pulling and charting ' + dir + scope + '…',
        statusKind: 'info', srcs: {}, report: null, browseEntries: [] });
      this.sshLog([{ text: '▦ analysing ' + dir + scope + ' …', color: C.info }], id);

      return SI.api.analyzeResults(id, dir, viz.caseName || '', window_)
        .then(function (report) {
          var notes = {};
          (report.charts || []).forEach(function (c) { notes[c.id] = c.note || ''; });
          self.vizPatch({
            busy: false, report: report, notes: notes, review: report.review || '',
            status: report.charts.length
              ? report.charts.length + ' chart(s) from ' + report.files.length +
                ' file(s)' + (scope ? ' — ' + vizWindowLabel(window_) + ' window' : '')
              : 'no charts: ' + ((report.warnings || [])[0] || 'nothing chartable'),
            statusKind: report.charts.length ? 'ok' : 'warn'
          });
          self.sshLog([{ text: '✓ ' + report.id + ' — ' + report.charts.length + ' chart(s)',
            color: C.ok }], id);
          // Point the history bar at the day this run landed on, so a fresh
          // report is visible in it without hunting for the right chip.
          return self.vizLoadImages(report).then(function () {
            return self.vizLoadSaved(vizDay(report));
          });
        })
        .catch(function (e) {
          self.vizPatch({ busy: false, status: errText(e), statusKind: 'err' });
          self.sshLog([{ text: '✗ analyse: ' + errText(e), color: C.err }], id);
        });
    },

    vizSave: function () {
      var self = this;
      var viz = this.state.viz || {};
      if (!viz.report) return;
      this.vizPatch({ status: 'saving…', statusKind: 'info' });
      return SI.api.saveReportNotes(viz.report.id, viz.notes || {}, viz.review || '')
        .then(function (report) {
          self.vizPatch({
            report: report,
            status: 'saved ' + report.id, statusKind: 'ok'
          });
          self.sshLog([{ text: '✓ saved report ' + report.id +
            ' (' + report.label + ' · ' + report.case_name + ')', color: C.ok }]);
          return self.vizLoadSaved(vizDay(report));
        })
        .catch(function (e) {
          self.vizPatch({ status: 'save: ' + errText(e), statusKind: 'err' });
        });
    },

    vizOpen: function (id) {
      var self = this;
      this.vizDropImages();
      this.vizPatch({ busy: true, srcs: {}, status: 'opening ' + id + '…', statusKind: 'info' });
      return SI.api.getReport(id)
        .then(function (report) {
          var notes = {};
          (report.charts || []).forEach(function (c) { notes[c.id] = c.note || ''; });
          // The boxes are put back the way this report was made, window and
          // all -- so "re-run this with one more chart" is Analyze, not a hunt
          // through the manifest for which slice it was.
          var slice = report.window || {};
          self.vizPatch({
            busy: false, report: report, notes: notes,
            review: report.review || '', dir: report.source_path || '',
            caseName: report.case_name || '',
            windowOn: slice.label != null && slice.start != null,
            windowStart: slice.start != null ? String(slice.start) : undefined,
            windowEnd: slice.end != null ? String(slice.end) : undefined,
            status: report.label + ' · ' + report.case_name +
              (slice.label ? ' · ' + slice.label : ''),
            statusKind: 'ok'
          });
          return self.vizLoadImages(report);
        })
        .catch(function (e) {
          self.vizPatch({ busy: false, status: errText(e), statusKind: 'err' });
        });
    },

    /* Delete a saved report.
     *
     * This removes the whole folder on the backend -- the chart PNGs, the
     * manifest, the notes written against each chart, the overall review and
     * the logs kept for re-drawing. None of it is recoverable from here, and
     * the ✕ sits a few pixels from the chip that merely opens the report.
     *
     * `window.confirm` rather than a styled modal, for the reason `removeStage`
     * gives: a second confirmation style for the same class of action is worse
     * than a plain one. */
    vizDelete: function (id) {
      var self = this;
      var viz = this.state.viz || {};
      var row = (viz.saved || []).filter(function (s) { return s.id === id; })[0] || {};

      var message = 'Delete the report "' + (row.case_name || id) + '"' +
        (row.label ? ' from ' + row.label : '') + '?';
      var goes = [];
      if (row.charts) goes.push(row.charts + ' chart(s)');
      if (row.notes) goes.push(row.notes + ' note(s)');
      if (row.reviewed && !row.notes) goes.push('your review');
      if (goes.length) message += '\n\n' + goes.join(' and ') + ' go with it.';
      message += '\n\nThe report folder is removed from the server, including ' +
        'the logs its charts were drawn from. This cannot be undone.';
      if (!window.confirm(message)) return;

      return SI.api.deleteReport(id)
        .then(function () {
          // A pinned slot whose folder has gone would draw a column of broken
          // images against a report that no longer exists.
          if (((self.state.viz || {}).compare || []).indexOf(id) >= 0) {
            self.vizCompareDrop(id);
          }
          var open = (self.state.viz || {}).report;
          var patch = { status: 'deleted ' + id, statusKind: 'ok' };
          if (open && open.id === id) {
            // The report on screen is the one that just went. Clear it rather
            // than leaving a gallery whose backing folder no longer exists.
            self.vizDropImages();
            patch.report = null;
            patch.srcs = {};
            patch.notes = {};
            patch.review = '';
            patch.drafts = {};
            patch.configFor = '';
          }
          self.vizPatch(patch);
          self.sshLog([{ text: '✓ deleted report ' + id, color: C.ok }]);
          // Re-list rather than splicing locally: the day chips carry counts,
          // and deleting the last report of a day has to remove its chip too.
          return self.vizLoadSaved();
        })
        .catch(function (e) {
          self.vizPatch({ status: 'delete: ' + errText(e), statusKind: 'err' });
        });
    },

    /* ---- compare: two or three reports at once ---------------------------
     *
     * A run is only ever read against another run -- "is the split faster",
     * "did dynamic batching cost accuracy" -- and answering that by opening one
     * report, remembering a number, and opening the next is how a difference
     * gets misremembered. So compare mode pins up to three reports and draws
     * them chart for chart, row by row.
     *
     * It is a mode rather than a second tab because it reuses everything the
     * tab already has: the same History bar picks the reports, and clicking a
     * run adds it to a slot instead of opening it. Turning the mode off puts
     * the single-report gallery back exactly as it was.
     */
    vizCompareToggle: function () {
      var viz = this.state.viz || {};
      var on = !viz.compareOn;
      this.vizPatch({ compareOn: on, cmpFocus: '' });
      // Entering with a report open takes it as the first slot: that report is
      // almost always one half of the comparison being reached for.
      if (on && viz.report && !(viz.compare || []).length) {
        return this.vizCompareAdd(viz.report.id);
      }
    },

    vizCompareToggleReport: function (id) {
      var picked = ((this.state.viz || {}).compare || []).indexOf(id) >= 0;
      return picked ? this.vizCompareDrop(id) : this.vizCompareAdd(id);
    },

    vizCompareAdd: function (id) {
      var viz = this.state.viz || {};
      var picked = (viz.compare || []).slice();
      if (picked.indexOf(id) >= 0) return;
      if (picked.length >= VIZ_COMPARE_MAX) {
        this.vizPatch({
          status: 'three reports is the limit — drop one to add another',
          statusKind: 'warn'
        });
        return;
      }
      picked.push(id);
      this.vizPatch({ compare: picked, compareOn: true });
      return this.vizCompareLoad(id);
    },

    /* One slot's manifest and its chart PNGs.
     *
     * Fetched per slot rather than by extending the single-report state: the
     * open report and a pinned one are different things, and sharing `srcs`
     * between them means opening a report revokes an image the comparison is
     * still showing. */
    vizCompareLoad: function (id) {
      var self = this;
      var viz = this.state.viz || {};
      if ((viz.cmpReports || {})[id]) return Promise.resolve((viz.cmpReports || {})[id]);

      this.vizPatch({ cmpBusy: true });
      return SI.api.getReport(id)
        .then(function (report) {
          var reports = Object.assign({}, (self.state.viz || {}).cmpReports || {});
          reports[id] = report;
          self.vizPatch({ cmpReports: reports, cmpBusy: false });

          return Promise.all((report.charts || []).map(function (c) {
            return SI.api.chartImage(id, c.file, report.rendered_at || '')
              .then(function (src) {
                return imageShape(src).then(function (shape) {
                  return { id: c.id, src: src, shape: shape };
                });
              })
              .catch(function () { return { id: c.id, src: '', shape: null }; });
          })).then(function (pairs) {
            // Re-read rather than closing over the old map: three slots can be
            // loading at once and each lands whenever its images finish.
            var viz = self.state.viz || {};
            var srcs = Object.assign({}, viz.cmpSrcs || {});
            var shapes = Object.assign({}, viz.cmpShapes || {});
            var mine = {};
            var mineShapes = {};
            pairs.forEach(function (p) {
              mine[p.id] = p.src;
              if (p.shape) mineShapes[p.id] = p.shape;
            });
            srcs[id] = mine;
            shapes[id] = mineShapes;
            self.vizPatch({ cmpSrcs: srcs, cmpShapes: shapes });
            return report;
          });
        })
        .catch(function (e) {
          self.vizPatch({
            cmpBusy: false, status: 'compare: ' + errText(e), statusKind: 'err'
          });
          // Leaving a slot pinned to a report that would not load means a
          // permanently empty column with no way to read why.
          self.vizCompareDrop(id);
        });
    },

    vizCompareDrop: function (id) {
      var viz = this.state.viz || {};
      var srcs = Object.assign({}, viz.cmpSrcs || {});
      var mine = srcs[id] || {};
      Object.keys(mine).forEach(function (k) {
        try { URL.revokeObjectURL(mine[k]); } catch (e) { /* already gone */ }
      });
      delete srcs[id];
      var reports = Object.assign({}, viz.cmpReports || {});
      delete reports[id];
      var shapes = Object.assign({}, viz.cmpShapes || {});
      delete shapes[id];

      var left = (viz.compare || []).filter(function (x) { return x !== id; });
      var patch = {
        compare: left, cmpReports: reports, cmpSrcs: srcs, cmpShapes: shapes
      };
      // A chart focused because *that* report had it is no longer focusable.
      if (viz.cmpFocus && !vizCompareSpine(left, reports).filter(
        function (o) { return o.id === viz.cmpFocus; }).length) {
        patch.cmpFocus = '';
      }
      this.vizPatch(patch);
    },

    vizCompareClear: function () {
      var self = this;
      ((this.state.viz || {}).compare || []).slice().forEach(function (id) {
        self.vizCompareDrop(id);
      });
      this.vizPatch({ cmpFocus: '' });
    },

    /* Surf: step to the next chart with the columns still lined up.
     *
     * Wraps around, and from "all charts" ▶ lands on the first and ◀ on the
     * last -- so the arrow keys are a way *into* single-chart reading, not
     * something that only works once you are already there. */
    vizCompareStep: function (delta) {
      var viz = this.state.viz || {};
      var spine = vizCompareSpine(viz.compare, viz.cmpReports);
      if (!spine.length) return;
      var at = spine.map(function (o) { return o.id; }).indexOf(viz.cmpFocus || '');
      var next = at < 0 ? (delta > 0 ? 0 : spine.length - 1) : at + delta;
      if (next < 0) next = spine.length - 1;
      if (next >= spine.length) next = 0;
      this.vizPatch({ cmpFocus: spine[next].id });
    },

    /* The history bar's data. Everything is fetched once and filtered by day
     * in the browser: switching day is then instant, and the day chips can
     * keep showing counts for days that are not selected. */
    vizLoadSaved: function (preferDay) {
      var self = this;
      return SI.api.listReports(200)
        .then(function (r) {
          var days = r.days || [];
          var current = preferDay || (self.state.viz || {}).day || '';
          // Fall back to the newest day when the selected one has gone --
          // deleting the last report of a day would otherwise leave the bar
          // filtered to nothing with no way to tell why.
          var known = days.filter(function (d) { return d.day === current; }).length;
          self.vizPatch({
            saved: r.reports || [],
            days: days,
            day: known ? current : ((days[0] && days[0].day) || '')
          });
        })
        .catch(function () { /* the list is a convenience; never block on it */ });
    },

    sshServerTest: function () {
      if (!controlLive(this)) {
        mockNotice(this, 'the connection test');
        return base.sshServerTest.call(this);
      }
      var self = this;
      var sv = this.state.ssh.server;
      if (!sv.ip || !sv.user) {
        this.sshLog([{ text: '✗ fill in the IP and SSH user first', color: C.err }]);
        return;
      }

      this.sshServerPatch({ status: 'connecting' });
      this.sshLog([{ text: '⇄ ssh ' + sv.user + '@' + sv.ip + ':' + (sv.port || 22) + ' …',
        color: C.info }]);

      return SI.api.saveServerConfig({
        ip: sv.ip,
        ssh_port: Number(sv.port) || 22,
        ssh_user: sv.user,
        ssh_password: sv.password || undefined,
        jump_enabled: !!sv.jump,
        // Sent so a partial save cannot wipe them; unchanged values are no-ops.
        amqp_host: sv.amqpHost || '',
        port: Number(sv.amqpPort) || 5672,
        user: sv.amqpUser || 'guest',
        password: sv.amqpPassword || undefined
      })
        .then(function () {
          // Written; drop it from component state so it lives only in the
          // server's encrypted store, as exportJson() already assumes.
          self.sshServerPatch({ password: '', amqpPassword: '' });
          return SI.api.testServerSsh();
        })
        .then(function (r) {
          self.sshServerPatch({
            status: r.ok ? 'on' : 'error',
            banner: r.ok ? r.banner : (r.error || 'login failed'),
            bannerOk: r.ok
          });
          self.sshLog([{
            text: r.ok
              ? '✓ connected — ' + (r.banner || 'shell ready')
              : '✗ ' + (r.error || 'login failed'),
            color: r.ok ? C.ok : C.err
          }]);
          // The "now go select it" hint is guidance for the first connect;
          // repeated on every reconnect it is just noise in a log the operator
          // is trying to read command output from.
          if (r.ok && !self._siConnectedOnce) {
            self._siConnectedOnce = true;
            self.sshLog([{ text: '  select "' + r.user + '@' + r.host +
              '" under Targets and run a command', color: C.hint }]);
          }
        })
        .catch(function (e) {
          self.sshServerPatch({ status: 'error', banner: errText(e), bannerOk: false });
          self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
        });
    },

    /* The full three-leg check, behind the collapsed Broker section. */
    sshServerTestAll: function () {
      if (!controlLive(this)) {
        mockNotice(this, 'the connection test');
        return base.sshServerTest.call(this);
      }
      var self = this;
      var sv = this.state.ssh.server;
      this.sshServerPatch({ status: 'connecting' });
      this.sshLog([{ text: '⇄ checking ssh · amqp :' + (sv.amqpPort || 5672) +
        ' · control API …', color: C.info }]);

      return SI.api.saveServerConfig({
        ip: sv.ip,
        // The card's four main fields are the SSH login; AMQP has its own row.
        ssh_port: Number(sv.port) || 22,
        ssh_user: sv.user,
        ssh_password: sv.password || undefined,
        jump_enabled: !!sv.jump,
        // The broker is its own machine as far as this card is concerned.
        amqp_host: sv.amqpHost || '',
        port: Number(sv.amqpPort) || 5672,
        user: sv.amqpUser || 'guest',
        password: sv.amqpPassword || undefined
      })
        .then(function () {
          // Written; drop both from component state so they live only in the
          // server's encrypted store, as exportJson() already assumes.
          self.sshServerPatch({ password: '', amqpPassword: '' });
          return SI.api.testServerConnection();
        })
        .then(function (r) {
          // Three independent legs cannot honestly collapse into one dot, so
          // the card carries a per-leg summary and the dot reports the worst
          // of them. SSH failing is what makes it red -- that is the leg this
          // card exists to establish; a broker on another host is a
          // configuration choice, not a fault.
          var amqpOk = !r.broker_error;
          var apiOk = r.api === 'up';
          var sshOk = r.ssh === 'ok';
          var sshSkipped = r.ssh === 'skipped';
          var legs = 'ssh ' + (sshSkipped ? '–' : sshOk ? '✓' : '✗') +
                     ' · amqp ' + (amqpOk ? '✓' : '✗') +
                     ' · api ' + (apiOk ? '✓' : '✗');
          var detail = sshOk ? (r.ssh_banner || '') : (r.ssh_error || r.broker_error || '');
          self.sshServerPatch({
            status: (!sshSkipped && !sshOk) ? 'error' : (amqpOk && apiOk) ? 'on' : 'partial',
            banner: legs + (detail ? '  —  ' + detail : ''),
            bannerOk: sshOk || sshSkipped
          });
          if (r.ssh === 'ok') {
            self.sshLog([{ text: '✓ ssh ' + sv.user + '@' + sv.ip + ' — ' +
              (r.ssh_banner || 'logged in'), color: C.ok }]);
          } else if (r.ssh === 'failed') {
            self.sshLog([{ text: '✗ ssh: ' + (r.ssh_error || 'login failed'), color: C.err }]);
          } else {
            self.sshLog([{ text: '· ssh: skipped (no SSH user on the card)', color: C.hint }]);
          }
          self.sshLog([{
            text: r.broker_error
              ? '✗ amqp: ' + r.broker_error
              : '✓ amqp: ' + (r.product || 'RabbitMQ') + ' ' + (r.rabbitmq_version || '?'),
            color: r.broker_error ? C.err : C.ok
          }]);
          self.sshLog([{ text: (r.api === 'up' ? '✓' : '✗') + ' control API ' + r.api,
            color: r.api === 'up' ? C.ok : C.err }]);
        })
        .catch(function (e) {
          self.sshServerPatch({ status: 'error', banner: errText(e), bannerOk: false });
          self.sshLog([{ text: '✗ ' + errText(e), color: C.err }]);
        });
    },

    // --- render ----------------------------------------------------------
    renderVals: function () {
      var R = base.renderVals.call(this);
      var self = this;
      var st = this.state;
      var live = SI.isLive();

      // Header: mode pill, connection chip, Deploy.
      var chipColor = !st.siReachable ? 'var(--alert)'
        : (live && !st.siStreamUp) ? 'var(--server)' : 'var(--data)';
      R.siChip = {
        label: !st.siReachable ? 'backend offline'
          : live ? (st.siStreamUp ? 'streaming' : 'connecting…') : 'backend ready',
        dotStyle: { width: '8px', height: '8px', borderRadius: '50%', background: chipColor, flexShrink: 0 },
        style: {
          display: 'flex', alignItems: 'center', gap: '6px', background: 'var(--surface)',
          border: '1px solid var(--border)', borderRadius: '9px', padding: '7px 10px',
          fontSize: '11px', color: 'var(--muted)', fontWeight: 600, cursor: 'pointer'
        },
        onClick: function () { openSettings(function () { self.siPing(); if (SI.isLive()) self.siStartLive({ quiet: false }); }); }
      };
      R.siModeLabel = live ? 'Live' : 'Simulate';
      R.siModeStyle = {
        display: 'flex', alignItems: 'center', gap: '6px', border: 'none', borderRadius: '9px',
        padding: '8px 14px', fontSize: '12px', fontWeight: 700, cursor: 'pointer', color: '#fff',
        background: live ? 'var(--data)' : 'var(--broker)'
      };
      R.onSiToggleMode = function () { self.siToggleMode(); };
      R.siDeployStyle = {
        display: live ? 'flex' : 'none', alignItems: 'center', gap: '6px',
        background: 'var(--surface)', color: 'var(--ink)', border: '1px solid var(--border)',
        borderRadius: '9px', padding: '8px 12px', fontSize: '12px', fontWeight: 600, cursor: 'pointer'
      };
      R.onSiDeploy = function () { self.siDeploy(); };

      var anyBusy = this.siAnyMeasuring();
      var fleetCount = (st.stages || []).reduce(function (n, stage) {
        return n + ((stage.devices || []).length);
      }, 0);
      R.onSiMeasureAll = function () { self.siMeasureAll(); };
      R.siMeasureAllLabel = anyBusy ? 'measuring…' : '⟳ measure all';
      R.siMeasureAllTitle = 'Measure every device in every stage in one pass. '
        + 'Bandwidth is taken with all ' + fleetCount + ' publishing at once, which '
        + 'is what a run does — per-stage passes only contend within their own stage.';
      R.siMeasureAllStyle = {
        display: 'flex', alignItems: 'center', gap: '6px',
        background: 'var(--surface)', color: anyBusy ? 'var(--muted)' : 'var(--ink)',
        border: '1px solid var(--border)', borderRadius: '9px', padding: '8px 12px',
        fontSize: '12px', fontWeight: 600, whiteSpace: 'nowrap',
        cursor: anyBusy ? 'progress' : (fleetCount ? 'pointer' : 'not-allowed')
      };

      // --- measure button, in each stage's header bar ---
      // Present in Simulate mode too: which math draws the charts has nothing
      // to do with whether the hardware can be timed, and the specs it writes
      // are what Simulate mode goes on to compute *from*.
      var measuring = st.siMeasuring || {};
      var measured = st.siMeasured || {};
      R.stages = (R.stages || []).map(function (stage) {
        var busy = !!measuring[stage.id];
        var count = (stage.devices || []).length;
        var notice = measured[stage.id];
        return Object.assign({}, stage, self.siNoticeFor(notice, busy), {
          onMeasure: function () { self.siMeasureStage(stage.id); },
          measureLabel: busy ? 'measuring…' : '⟳ measure',
          measureTitle: count
            ? 'SSH into all ' + count + ' device(s) in ' + stage.name + ' and replace '
              + 'GFLOPS / MB/s / LAT MS with measured values. Bandwidth is taken one '
              + 'device at a time, then again with all of them at once.'
            : 'Add a device to this stage first',
          measureStyle: {
            // `flexShrink: 0` so the row never resolves the overflow by
            // squeezing this into "⟳ me" -- the stage-name box is the item
            // that is supposed to give up the space (see STAGE_NAME_INPUT in
            // tools/build_web.py), and it can only do that if nothing else
            // volunteers first.
            flexShrink: 0,
            height: '26px', padding: '0 8px', background: 'var(--bg)',
            border: '1px solid var(--border)', borderRadius: '7px',
            color: busy || !count ? 'var(--muted)' : stage.color,
            fontSize: '11px', fontWeight: 700, whiteSpace: 'nowrap',
            cursor: busy ? 'progress' : (count ? 'pointer' : 'not-allowed')
          }
        });
      });

      // Keep the File itself: the backend needs the bytes, the UI only tracked
      // the name.
      var innerOnScpFile = R.ssh.onScpFile;
      R.ssh.onScpFile = function (e) {
        var f = e.target.files && e.target.files[0];
        if (f) self._siScpFile = f;
        innerOnScpFile(e);
      };

      // --- pull / browse ---
      var pullId = this.siPullTarget();
      var pullDevice = this.flatDevices().filter(function (d) { return d.id === pullId; })[0];
      R.ssh.pullFromLabel = 'Pull from ' +
        (pullDevice ? pullDevice.name : (st.ssh.server.user ? 'control server' : 'server'));
      R.ssh.pullPath = st.ssh.pullPath || '';
      R.ssh.onPullPath = function (e) { self.sshPatch({ pullPath: e.target.value }); };
      R.ssh.onBrowse = function () { self.siBrowse(); };
      R.ssh.onPull = function () { self.siPull(); };
      R.ssh.browseStyle = {
        background: 'var(--bg)', color: 'var(--muted)', border: '1px solid var(--border)',
        borderRadius: '9px', padding: '9px 12px', fontSize: '12px', fontWeight: 600,
        cursor: st.ssh.browseBusy ? 'progress' : 'pointer'
      };
      R.ssh.pullStyle = {
        background: 'var(--cloud)', color: '#fff', border: 'none', borderRadius: '9px',
        padding: '9px 16px', fontSize: '12px', fontWeight: 700, cursor: 'pointer'
      };

      var entries = st.ssh.browseEntries || [];
      R.ssh.browseRowStyle = entries.length
        ? 'margin-top:8px; max-height:190px; overflow:auto; display:flex;' +
          'flex-direction:column; gap:1px; background:var(--bg); border:1px solid var(--border);' +
          'border-radius:9px; padding:5px;'
        : 'display:none;';
      R.ssh.browseEntries = entries.map(function (f) {
        return {
          name: (f.dir ? '📁 ' : '') + f.name,
          path: f.path,
          size: f.dir ? '' : fmtBytes(f.size),
          // Clicking a directory descends into it; clicking a file just fills
          // the box, so Pull stays a deliberate second action.
          onPick: function () {
            self.sshPatch({ pullPath: f.path });
            if (f.dir) self.siBrowse();
          },
          style: {
            display: 'flex', gap: '8px', alignItems: 'center', padding: '4px 7px',
            borderRadius: '6px', cursor: 'pointer', fontSize: '11px',
            fontFamily: 'ui-monospace, monospace',
            color: f.dir ? 'var(--edge)' : 'var(--ink)'
          }
        };
      });

      // --- visual panel: charts for a finished run ---
      // Targets the same device as the Files card above (the first selected),
      // so "pull from" and "analyze from" can never drift apart on screen.
      var viz = st.viz || {};
      var vizReport = viz.report;
      var vizCharts = (vizReport && vizReport.charts) || [];
      var vizSrcs = viz.srcs || {};
      var vizNotes = viz.notes || {};
      var vizSaved = viz.saved || [];
      var vizDays = viz.days || [];
      // No day chosen yet (first paint, before the listing lands) shows the
      // newest day rather than an empty bar.
      var vizPickedDay = viz.day || (vizDays[0] && vizDays[0].day) || '';
      var vizRuns = vizSaved.filter(function (s) { return s.day === vizPickedDay; });
      var vizStatusColor = {
        ok: 'var(--data)', err: 'var(--alert)', warn: 'var(--server)', info: 'var(--muted)'
      }[viz.statusKind || 'info'] || 'var(--muted)';

      // Compare mode changes what a History pill *does*, so the selection has
      // to be known before the pills are built.
      var cmpOn = !!viz.compareOn;
      var cmpReports = viz.cmpReports || {};
      var cmpSrcs = viz.cmpSrcs || {};
      var cmpIds = (viz.compare || []).slice(0, VIZ_COMPARE_MAX);
      var cmpSlotOf = function (id) { return cmpIds.indexOf(id); };

      /* One accent per chart form, so a gallery scanned at speed groups by what
       * a chart *is* before its title is read. These are UI chrome, not series
       * colors -- the marks inside every PNG come from the guide's own palette
       * and never from this table. */
      var VIZ_KIND = {
        trend: 'var(--edge)', comparison: 'var(--cloud)', distribution: 'var(--server)',
        delta: 'var(--data)', breakdown: 'var(--broker)'
      };

      /* Which cards take the whole row.
       *
       * A timeline needs the full width to read and the backend says so
       * (`wide`, measured off the figure). Two narrow charts sit side by side.
       * The case worth the loop is a narrow chart whose neighbour is wide: on
       * its own in a two-column grid it leaves half a row empty, so it takes
       * the row too. Pairing is done here rather than in CSS because the grid
       * cannot see the sequence, and `grid-auto-flow: dense` would fix the hole
       * by reordering the charts -- which throws away the narrative order the
       * catalogue numbers exist to keep. */
      /* Can this report's charts be re-drawn?
       *
       * Only if its logs were kept beside it. Reports analysed before that was
       * the case cannot be, and the config panel says so rather than letting
       * Apply travel to the backend and come back a 409. */
      var vizRedrawable = !!(((vizReport || {}).pulled || {}).kept);

      var vizFullRow = (function (charts) {
        var out = [], i = 0;
        while (i < charts.length) {
          if (!charts[i].wide && i + 1 < charts.length && !charts[i + 1].wide) {
            out[i] = out[i + 1] = false;
            i += 2;
          } else {
            out[i] = true;
            i += 1;
          }
        }
        return out;
      })(vizCharts);

      R.viz = {
        headline: vizReport
          ? (vizReport.case_name || vizReport.id)
          : (vizSaved.length ? vizSaved.length + ' saved report(s)' : 'no report open'),
        headlineSub: vizReport
          ? vizReport.label + ' · ' + vizCharts.length + ' chart(s)' +
            (((vizReport.window || {}).label)
              ? ' · ' + vizReport.window.label + ' of the run' : '')
          : 'pick a directory below, or a run from history',
        headlineStyle: {
          textAlign: 'right', minWidth: 0, maxWidth: '260px',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'
        },

        fromLabel: 'From ' +
          (pullDevice ? pullDevice.name : (st.ssh.server.user ? 'control server' : 'server')),
        dir: viz.dir || '',
        onDir: function (e) { self.vizPatch({ dir: e.target.value }); },
        onBrowse: function () { self.vizBrowse(); },
        browseStyle: {
          background: 'var(--bg)', color: 'var(--muted)', border: '1px solid var(--border)',
          borderRadius: '9px', padding: '9px 12px', fontSize: '12px', fontWeight: 600,
          cursor: viz.browseBusy ? 'progress' : 'pointer'
        },
        browseRowStyle: (viz.browseEntries || []).length
          ? 'max-height:170px; overflow:auto; display:flex; flex-direction:column; gap:1px;' +
            'background:var(--bg); border:1px solid var(--border); border-radius:9px; padding:5px;'
          : 'display:none;',
        browseEntries: (viz.browseEntries || []).map(function (f) {
          return {
            name: (f.dir ? '📁 ' : '') + f.name,
            path: f.path,
            size: f.dir ? '' : fmtBytes(f.size),
            // A directory is the thing being analysed, so clicking one selects
            // it *and* descends -- the box always holds the level you can see.
            onPick: function () {
              self.vizPatch({ dir: f.path });
              if (f.dir) self.vizBrowse();
            },
            style: {
              display: 'flex', gap: '8px', alignItems: 'center', padding: '4px 7px',
              borderRadius: '6px', cursor: 'pointer', fontSize: '11px',
              fontFamily: 'ui-monospace, monospace',
              color: f.dir ? 'var(--edge)' : 'var(--muted)'
            }
          };
        }),

        caseName: viz.caseName || '',
        onCaseName: function (e) { self.vizPatch({ caseName: e.target.value }); },

        // The window row: off by default, because the whole run is what a
        // report has always meant and a slice has to be asked for.
        windowOn: !!viz.windowOn,
        onWindowToggle: function () {
          self.vizPatch({ windowOn: !(self.state.viz || {}).windowOn });
        },
        windowToggleLabel: viz.windowOn ? '⧗ Window' : '⧗ Whole run',
        windowToggleTitle: viz.windowOn
          ? 'Analysing part of the run — click to go back to all of it'
          : 'Analysing the whole run — click to chart only part of it',
        windowToggleStyle: {
          background: viz.windowOn ? 'var(--server)' : 'var(--bg)',
          color: viz.windowOn ? '#fff' : 'var(--muted)',
          border: '1px solid ' + (viz.windowOn ? 'var(--server)' : 'var(--border)'),
          borderRadius: '9px', padding: '9px 12px', fontSize: '12px',
          fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap'
        },
        windowRowStyle: viz.windowOn ? '' : 'display:none;',
        windowStart: vizWindowText(viz, 'windowStart'),
        windowEnd: vizWindowText(viz, 'windowEnd'),
        onWindowStart: function (e) { self.vizPatch({ windowStart: e.target.value }); },
        onWindowEnd: function (e) { self.vizPatch({ windowEnd: e.target.value }); },
        // Says what the two numbers will do to *this* run rather than in
        // general, and says the limit out loud: the summary files hold one
        // whole-run line each, and no window can re-cut those.
        windowHint: (function () {
          var w = vizWindowOf(viz);
          if (!w) return '';
          if (w.error) return '✗ ' + w.error;
          return vizWindowLabel(w) + ' of the run — batches ' + Math.ceil(w.start) +
            ' to ' + Math.floor(w.end) + ' of 100, on every per-batch chart. ' +
            'The throughput, latency, utilization and accuracy summaries are ' +
            'whole-run figures and stay that way.';
        })(),
        windowHintStyle: (function () {
          var w = vizWindowOf(viz);
          if (!w) return 'display:none;';
          return 'font-size:10px; line-height:1.5; color:' +
            (w.error ? 'var(--alert)' : 'var(--muted)') + ';';
        })(),

        busy: !!viz.busy,
        onAnalyze: function () { self.vizAnalyze(); },
        analyzeLabel: viz.busy ? '… working' : '▦ Analyze',
        analyzeStyle: {
          background: viz.busy ? 'var(--border)' : 'var(--edge)', color: '#fff',
          border: 'none', borderRadius: '9px', padding: '9px 16px', fontSize: '12px',
          fontWeight: 700, cursor: viz.busy ? 'progress' : 'pointer'
        },
        status: viz.status || '',
        statusStyle: viz.status
          ? 'font-size:10px; font-family:ui-monospace,monospace; color:' + vizStatusColor + ';'
          : 'display:none;',

        // C12: the numbers that *are* the story, above the charts. The delta
        // line carries the words as well as the color -- a status hue on its
        // own is never the whole reading (§1).
        tileRowStyle: ((vizReport && vizReport.tiles) || []).length ? '' : 'display:none;',
        tiles: ((vizReport && vizReport.tiles) || []).map(function (t) {
          var accent = t.delta_kind === 'bad' ? 'var(--alert)'
            : t.delta_kind === 'good' ? 'var(--data)' : 'var(--edge)';
          return {
            label: t.label,
            value: t.value,
            unit: t.unit || '',
            // Older reports predate `delta`; the source is the useful fallback.
            delta: t.delta || t.source || '',
            source: t.source || '',
            deltaStyle: t.delta_kind
              ? 'color:' + accent + '; font-weight:600;'
              : '',
            railStyle: 'position:absolute; left:0; top:0; bottom:0; width:3px;' +
              'background:' + accent + ';'
          };
        }),

        emptyStyle: (vizReport && !vizCharts.length) || (!vizReport && !viz.busy)
          ? '' : 'display:none;',
        emptyText: vizReport
          ? ('Nothing chartable in that directory — ' +
             ((vizReport.warnings || [])[0] || 'the logs held no measurements.'))
          : 'No report open. Point at a result directory and press Analyze, or ' +
            'reopen one from History.',

        galleryStyle: vizCharts.length ? '' : 'display:none;',
        charts: vizCharts.map(function (c, i) {
          var full = vizFullRow[i];
          var accent = VIZ_KIND[c.kind] || 'var(--muted)';
          var open = viz.configFor === c.id;
          var stored = c.view || {};
          var draft = (viz.drafts || {})[c.id] || {
            title: stored.title || '', xlabel: stored.xlabel || '',
            ylabel: stored.ylabel || '', hidden: (stored.hidden || []).slice()
          };
          var hidden = draft.hidden || [];
          var series = c.series || [];
          var edited = !!(stored.title || stored.xlabel || stored.ylabel ||
            (stored.hidden || []).length);
          // Set equality, not a joined string: series keys contain spaces
          // ("Cluster 0"), so any separator-based compare is ambiguous.
          var was = stored.hidden || [];
          var dirty = draft.title !== (stored.title || '') ||
            draft.xlabel !== (stored.xlabel || '') ||
            draft.ylabel !== (stored.ylabel || '') ||
            hidden.length !== was.length ||
            hidden.some(function (k) { return was.indexOf(k) < 0; });
          var defaults = c.defaults || {};
          return {
            onConfig: function () { self.vizConfigOpen(c.id); },
            configTitle: open ? 'Close the chart settings'
              : 'Choose which series to show, and rename the title and axes',
            gearStyle: open
              ? 'background:var(--bg); border:1px solid var(--data); border-radius:7px;' +
                'color:var(--data); font-size:13px; line-height:1; padding:3px 6px;' +
                'cursor:pointer; opacity:1;'
              // An edited chart wears its accent on the gear, so "this one was
              // changed from the guide's default" is visible without opening it.
              : (edited
                 ? 'background:none; border:1px solid ' + accent + '; border-radius:7px;' +
                   'color:' + accent + '; font-size:13px; line-height:1; padding:3px 7px;' +
                   'cursor:pointer; opacity:1;'
                 : ''),
            configStyle: open ? '' : 'display:none;',
            // Hidden series stay listed and switchable: a chart you cannot get
            // a series back into is a chart you have broken.
            seriesList: series.map(function (s) {
              var off = hidden.indexOf(s.key) >= 0;
              return {
                label: s.label,
                title: off ? 'Hidden — click to show' : 'Shown — click to hide',
                onToggle: function () { self.vizToggleSeries(c.id, s.key); },
                style: {
                  display: 'inline-flex', alignItems: 'center', gap: '6px',
                  background: off ? 'transparent' : 'var(--raised)',
                  border: '1px solid ' + (off ? 'var(--border)' : accent),
                  borderRadius: '999px', padding: '3px 10px', fontSize: '10.5px',
                  fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap',
                  color: off ? 'var(--muted)' : 'var(--ink)',
                  textDecoration: off ? 'line-through' : 'none'
                },
                // The swatch is the identity cue; the series colour never goes
                // on the text itself (§1).
                dotStyle: 'width:9px; height:9px; border-radius:3px; flex-shrink:0;' +
                  'background:' + (s.color || 'var(--muted)') + ';' +
                  (off ? 'opacity:.3;' : '')
              };
            }),
            noSeriesStyle: series.length ? 'display:none;'
              : 'font-size:10.5px; color:var(--muted);',
            draftTitle: draft.title,
            draftX: draft.xlabel,
            draftY: draft.ylabel,
            // Placeholders are the guide's own wording, so Reset is visible
            // rather than guessed at.
            titleHint: defaults.title || 'chart title',
            xHint: defaults.xlabel || '(no x-axis label)',
            yHint: defaults.ylabel || '(no y-axis label)',
            onTitle: function (e) { self.vizDraft(c.id, { title: e.target.value }); },
            onX: function (e) { self.vizDraft(c.id, { xlabel: e.target.value }); },
            onY: function (e) { self.vizDraft(c.id, { ylabel: e.target.value }); },
            onApply: function () {
              if (vizRedrawable) self.vizApplyViews();
            },
            onReset: function () {
              if (vizRedrawable) self.vizResetChart(c.id);
            },
            applyLabel: viz.busy ? '… redrawing' : '↻ Apply',
            applyStyle: {
              background: dirty && vizRedrawable && !viz.busy
                ? 'var(--data)' : 'var(--border)',
              color: dirty && vizRedrawable && !viz.busy ? '#fff' : 'var(--muted)',
              border: 'none', borderRadius: '8px', padding: '7px 13px',
              fontSize: '11px', fontWeight: 700,
              cursor: !vizRedrawable ? 'not-allowed'
                : (viz.busy ? 'progress' : 'pointer'),
              whiteSpace: 'nowrap'
            },
            resetStyle: {
              background: 'none', border: '1px solid var(--border)',
              borderRadius: '8px', padding: '7px 11px', fontSize: '11px',
              fontWeight: 600, color: 'var(--muted)',
              cursor: vizRedrawable ? 'pointer' : 'not-allowed'
            },
            configHint: !vizRedrawable
              ? 'this report was saved without its logs — re-run Analyze on the ' +
                'same directory to make its charts configurable'
              : (dirty
                 ? 'unsaved — Apply redraws the chart on the backend'
                 : (hidden.length
                    ? hidden.length + ' of ' + series.length + ' series hidden'
                    : 'drawn to the visual guide defaults')),
            // The saved file name carries the catalogue number, and that
            // number is stable across re-runs -- so it is the one to show,
            // not this row's position in the list.
            index: (c.file || '').slice(0, 2) || String(i + 1),
            title: c.title,
            subtitle: c.subtitle || '',
            subtitleStyle: c.subtitle
              ? 'font-size:10.5px; color:var(--muted); line-height:1.45; margin:0 0 9px;'
              : 'display:none;',
            kind: c.kind || '',
            summary: c.summary || '',
            summaryStyle: c.summary
              ? 'font-size:10.5px; color:var(--muted); line-height:1.5; margin-top:9px;'
              : 'display:none;',
            cardStyle: full ? 'grid-column:1 / -1;' : '',
            // A narrow chart on a full row keeps its own width rather than
            // being stretched to a size the figure was never drawn for.
            imgStyle: full && !c.wide ? 'max-width:820px;' : '',
            chipStyle: 'color:' + accent + '; border:1px solid ' + accent + ';',
            src: vizSrcs[c.id] || BLANK_IMG,
            note: vizNotes[c.id] || '',
            onNote: function (e) {
              var next = Object.assign({}, (self.state.viz || {}).notes || {});
              next[c.id] = e.target.value;
              self.vizPatch({ notes: next });
            }
          };
        }),

        review: viz.review || '',
        onReview: function (e) { self.vizPatch({ review: e.target.value }); },
        saveRowStyle: vizReport
          ? 'display:grid; grid-template-columns:auto 1fr auto; gap:9px; align-items:center;'
          : 'display:none;',
        onSave: function () { self.vizSave(); },
        saveLabel: (vizReport && vizReport.saved_at) ? '✓ Save again' : '⇩ Save report',
        saveStyle: {
          background: 'var(--data)', color: '#fff', border: 'none', borderRadius: '9px',
          padding: '9px 16px', fontSize: '12px', fontWeight: 700, cursor: 'pointer',
          whiteSpace: 'nowrap'
        },

        // --- history bar: a day picker, then that day's runs ---
        historyStyle: vizSaved.length
          ? 'display:flex; flex-direction:column; gap:9px;'
          : 'display:none;',
        days: vizDays.map(function (d) {
          var active = d.day === vizPickedDay;
          return {
            label: d.label + ' · ' + d.count,
            title: d.day + ' — ' + d.count + ' report(s)',
            onPick: function () { self.vizPatch({ day: d.day }); },
            style: {
              background: active ? 'var(--edge)' : 'var(--bg)',
              color: active ? '#fff' : 'var(--muted)',
              border: '1px solid ' + (active ? 'var(--edge)' : 'var(--border)'),
              borderRadius: '999px',
              padding: '4px 11px', fontSize: '10px', fontWeight: 700,
              cursor: 'pointer', whiteSpace: 'nowrap'
            }
          };
        }),
        runRowStyle: vizRuns.length
          ? 'display:flex; gap:7px; align-items:center; flex-wrap:wrap;' +
            'border-top:1px solid var(--border); padding-top:9px;'
          : 'display:none;',
        runs: vizRuns.map(function (s) {
          // In compare mode a pill is a slot toggle, not a way in: the whole
          // point of the mode is holding several of these at once, and a click
          // that swapped the open report would undo the last one every time.
          var slot = cmpSlotOf(s.id);
          var active = cmpOn ? slot >= 0 : !!(vizReport && vizReport.id === s.id);
          var accent = cmpOn && slot >= 0 ? VIZ_SLOT[slot] : 'var(--data)';
          return {
            // The note pip is the point of keeping history: it says at a
            // glance which past runs were actually reviewed.
            // …and the window marker says which pills are a slice of a run
            // rather than all of it. Two analyses of one directory that differ
            // only by their window are otherwise the same pill twice.
            label: (cmpOn && slot >= 0 ? VIZ_SLOT_MARK[slot] + ' ' : '') +
              s.time_label + '  ' + s.case_name +
              (s.window ? '  ⧗' + s.window : '') + (s.reviewed ? '  ✎' : ''),
            title: (cmpOn
                    ? (slot >= 0 ? 'Slot ' + (slot + 1) + ' — click to unpin\n'
                                 : 'Click to pin beside the others\n')
                    : '') +
              s.id + ' — ' + s.charts + ' chart(s)' +
              (s.notes ? ', ' + s.notes + ' note(s)' : ', no notes') +
              (s.window ? '\nanalysed over ' + s.window + ' of the run' : '') +
              (s.device_name ? '\n' + s.device_name : '') +
              (s.source_path ? '\n' + s.source_path : ''),
            onOpen: cmpOn
              ? function () { self.vizCompareToggleReport(s.id); }
              : function () { self.vizOpen(s.id); },
            onDelete: function () { self.vizDelete(s.id); },
            deleteTitle: 'Delete this report — asks first',
            // The pill is the wrapper, so opening and deleting are two targets
            // inside one control rather than two chips that look unrelated.
            // A <button> cannot nest inside a <button>.
            wrapStyle: {
              background: active ? accent : 'var(--bg)',
              color: active ? '#fff' : 'var(--ink)',
              border: '1px solid ' + (active ? accent : 'var(--border)'),
              borderRadius: '9px', fontSize: '10.5px', fontWeight: 600,
              fontFamily: 'ui-monospace, monospace'
            }
          };
        })
      };

      /* ---- compare mode: two or three reports, chart for chart -------------
       *
       * The unit is the row: one chart, drawn from every pinned report, side by
       * side. Matched by the chart's catalogue id, never by position -- a run
       * with no `map.log` is missing charts 09 and 10, and lining two galleries
       * up by index would put its 08 beside the other's 09 and invite a reading
       * of the difference that is really a reading of the misalignment. A
       * report that has no such chart gets a stated gap in its own column.
       *
       * Every row on the page -- the numbers table, the column headers, each
       * chart -- is laid out on one shared grid template, so a column is the
       * same report from the top of the block to the bottom. That is all
       * "synchronised" has to mean here: nothing needs to scroll in step
       * because nothing is ever out of step.
       */
      var cmpSlots = cmpIds.map(function (id, i) {
        var rep = cmpReports[id] || {};
        var row = vizSaved.filter(function (s) { return s.id === id; })[0] || {};
        return {
          id: id,
          accent: VIZ_SLOT[i] || 'var(--muted)',
          name: rep.case_name || row.case_name || id,
          when: rep.label || row.label || '',
          loaded: !!rep.id
        };
      });
      var cmpSpine = vizCompareSpine(cmpIds, cmpReports);
      var cmpFocus = viz.cmpFocus || '';
      var cmpAt = cmpSpine.map(function (o) { return o.id; }).indexOf(cmpFocus);
      if (cmpFocus && cmpAt < 0) cmpFocus = '';      // its report was unpinned
      var cmpReady = cmpSlots.length >= 2;

      // The one template every row shares. The first column is the row's own
      // label -- chart title, or tile name -- so the numbers table and the
      // charts line up with each other and not merely with themselves.
      var cmpGrid = 'display:grid; gap:10px; align-items:start;' +
        'grid-template-columns:minmax(104px, 168px) repeat(' +
        Math.max(1, cmpSlots.length) + ', minmax(0, 1fr));';

      var cmpShapes = viz.cmpShapes || {};

      var cmpRows = (cmpFocus
        ? cmpSpine.filter(function (o) { return o.id === cmpFocus; })
        : cmpSpine
      ).map(function (o) {
        var accent = VIZ_KIND[o.kind] || 'var(--muted)';

        /* One box shape for every cell in this row.
         *
         * The columns are already the same width, which is not enough on its
         * own: the guide draws a figure as wide as its categories need, so the
         * same chart is 1829px across in one run and 2271px in another, and
         * fitting each to the column draws one of them 25% larger. Sized to the
         * widest figure in the row and fitted with `object-fit:contain`, every
         * cell instead gets an identical box: the widest fills it, the others
         * are letterboxed into it, and all of them come out at one scale --
         * which is the only way a difference between two bars is a difference
         * in the run rather than in the rendering.
         *
         * Falls back to the plain column-width layout when the shapes are not
         * known yet (the images are still arriving) or could not be measured. */
        var aspects = cmpSlots.map(function (sl) {
          var shape = (cmpShapes[sl.id] || {})[o.id];
          return shape ? shape.w / shape.h : 0;
        }).filter(function (a) { return a > 0; });
        var widest = aspects.length ? Math.max.apply(null, aspects) : 0;
        var figStyle = widest ? 'aspect-ratio:' + widest.toFixed(4) + ';' : '';

        return {
          index: o.key || '',
          title: o.title,
          kind: o.kind || '',
          chipStyle: 'color:' + accent + '; border:1px solid ' + accent + ';',
          // One chart with the page to itself gets the height back: the cap
          // exists to keep a stack of ten rows scannable, and there is no stack
          // to scan here.
          gridStyle: cmpGrid + (cmpFocus ? '--viz-cmp-img:min(68vh, 720px);' : ''),
          onFocus: function () { self.vizPatch({ cmpFocus: cmpFocus ? '' : o.id }); },
          focusLabel: cmpFocus ? '▦ all' : '⤢ only this',
          focusTitle: cmpFocus
            ? 'Back to every chart'
            : 'Read this one chart across the reports, with ← → to move on',
          cells: cmpSlots.map(function (sl) {
            var chart = (((cmpReports[sl.id] || {}).charts) || []).filter(
              function (c) { return c.id === o.id; })[0];
            var note = (chart && chart.note) || '';
            var summary = (chart && chart.summary) || '';
            return {
              label: sl.name,
              tagStyle: 'color:' + sl.accent + ';',
              dotStyle: 'width:8px; height:8px; border-radius:3px; flex-shrink:0;' +
                'background:' + sl.accent + ';',
              imgStyle: chart ? '' : 'display:none;',
              figStyle: figStyle,
              src: (chart && (cmpSrcs[sl.id] || {})[chart.id]) || BLANK_IMG,
              title: chart ? chart.title : (sl.name + ' has no ' + o.title),
              // Named, not blank: "this report does not have that chart" is a
              // finding about the run, not a hole in the page.
              missStyle: chart ? 'display:none;' : '',
              missText: 'not in this report',
              // The written record travels with the chart it was written
              // against -- reading three notes side by side is most of why
              // anyone keeps them.
              note: note,
              noteStyle: note ? '' : 'display:none;',
              // Only when one chart has the page: three summaries at gallery
              // size is more prose than chart.
              summary: cmpFocus ? summary : '',
              summaryStyle: (cmpFocus && summary) ? '' : 'display:none;'
            };
          })
        };
      });

      /* The headline numbers, one row per tile, matched by label.
       *
       * Rows that read the same in every column are dimmed rather than
       * dropped: "batch size was 32 in both" is the context that makes the
       * rows which *do* differ mean something, and hiding it would leave the
       * reader unsure whether it was equal or simply missing. */
      var cmpTiles = (function () {
        var seen = {};
        var labels = [];
        cmpSlots.forEach(function (sl) {
          ((cmpReports[sl.id] || {}).tiles || []).forEach(function (t) {
            if (seen[t.label]) return;
            seen[t.label] = 1;
            labels.push(t.label);
          });
        });
        return labels.map(function (label) {
          var found = cmpSlots.map(function (sl) {
            return ((cmpReports[sl.id] || {}).tiles || []).filter(
              function (t) { return t.label === label; })[0] || null;
          });
          var shown = found.filter(Boolean).map(function (t) {
            return (t.value || '') + ' ' + (t.unit || '');
          });
          var same = shown.length === found.length && shown.every(
            function (v) { return v === shown[0]; });
          return {
            label: label,
            rowStyle: cmpGrid + (same ? ' opacity:.45;' : ''),
            cells: found.map(function (t) {
              return {
                value: t ? t.value : '—',
                unit: t ? (t.unit || '') : '',
                valueStyle: t ? '' : 'color:var(--muted);',
                delta: t ? (t.delta || t.source || '') : 'not measured here',
                deltaStyle: (t && t.delta_kind)
                  ? 'color:' + (t.delta_kind === 'bad' ? 'var(--alert)'
                    : t.delta_kind === 'good' ? 'var(--data)' : 'var(--edge)') +
                    '; font-weight:600;'
                  : ''
              };
            })
          };
        });
      })();

      R.viz.cmp = {
        on: cmpOn,
        onToggle: function () { self.vizCompareToggle(); },
        toggleLabel: cmpOn ? '✓ Comparing' : '⧉ Compare runs',
        toggleTitle: cmpOn
          ? 'Leave compare mode — the single-report gallery comes back'
          : 'Pin two or three runs from History and read them side by side',
        toggleStyle: {
          background: cmpOn ? 'var(--data)' : 'var(--bg)',
          color: cmpOn ? '#fff' : 'var(--muted)',
          border: '1px solid ' + (cmpOn ? 'var(--data)' : 'var(--border)'),
          borderRadius: '999px', padding: '4px 12px', fontSize: '10px',
          fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap'
        },

        cardStyle: cmpOn ? '' : 'display:none;',
        hint: !cmpSlots.length
          ? 'click runs in History to pin them — two or three'
          : (cmpSlots.length < 2
             ? 'one pinned — pin another run to compare it against'
             : cmpSlots.length + ' reports · ' + cmpSpine.length +
               ' chart(s) aligned by catalogue number' +
               (viz.cmpBusy ? ' · loading…' : '')),
        onClear: function () { self.vizCompareClear(); },
        clearStyle: {
          background: 'none', border: '1px solid var(--border)', borderRadius: '8px',
          color: 'var(--muted)', padding: '3px 10px', fontSize: '10px',
          fontWeight: 600, cursor: cmpSlots.length ? 'pointer' : 'not-allowed'
        },

        headStyle: cmpSlots.length ? cmpGrid : 'display:none;',
        slots: cmpSlots.map(function (sl) {
          return {
            name: sl.name,
            when: sl.when + (sl.loaded ? '' : ' · loading…'),
            onRemove: function () { self.vizCompareDrop(sl.id); },
            removeTitle: 'Unpin this report — the report itself is untouched',
            style: 'display:flex; align-items:center; gap:7px; min-width:0;' +
              'background:var(--bg); border:1px solid ' + sl.accent + ';' +
              'border-radius:9px; padding:6px 4px 6px 9px; font-size:11px;',
            dotStyle: 'width:9px; height:9px; border-radius:3px; flex-shrink:0;' +
              'background:' + sl.accent + ';'
          };
        }),

        // --- the surf controls: chips for every chart, arrows to step ---
        pickerStyle: cmpReady ? '' : 'display:none;',
        position: cmpFocus
          ? 'chart ' + (cmpAt + 1) + ' of ' + cmpSpine.length + '  ·  ← →'
          : cmpSpine.length + ' charts  ·  ← → to read one at a time',
        onPrev: function () { self.vizCompareStep(-1); },
        onNext: function () { self.vizCompareStep(1); },
        stepStyle: {
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: '8px', color: 'var(--ink)', padding: '4px 11px',
          fontSize: '11px', fontWeight: 700, cursor: 'pointer'
        },
        onAll: function () { self.vizPatch({ cmpFocus: '' }); },
        allStyle: {
          background: cmpFocus ? 'var(--bg)' : 'var(--data)',
          color: cmpFocus ? 'var(--muted)' : '#fff',
          border: '1px solid ' + (cmpFocus ? 'var(--border)' : 'var(--data)'),
          borderRadius: '999px', padding: '4px 11px', fontSize: '10px',
          fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0
        },
        picker: cmpSpine.map(function (o) {
          var active = o.id === cmpFocus;
          var accent = VIZ_KIND[o.kind] || 'var(--muted)';
          // Which reports actually have it, said on the chip: a chart only one
          // of three runs drew is a different thing to compare.
          var have = cmpSlots.filter(function (sl) {
            return (((cmpReports[sl.id] || {}).charts) || []).some(
              function (c) { return c.id === o.id; });
          }).length;
          return {
            label: (o.key ? o.key + ' ' : '') + o.title +
              (have < cmpSlots.length ? '  (' + have + '/' + cmpSlots.length + ')' : ''),
            title: o.title + ' — in ' + have + ' of ' + cmpSlots.length + ' report(s)',
            onPick: function () { self.vizPatch({ cmpFocus: active ? '' : o.id }); },
            style: {
              background: active ? accent : 'var(--bg)',
              color: active ? '#fff' : 'var(--muted)',
              border: '1px solid ' + (active ? accent : 'var(--border)'),
              borderRadius: '999px', padding: '4px 11px', fontSize: '10px',
              fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap',
              // Never shrunk: twenty chips in a flex row squeeze to "0…" each,
              // and a map of the comparison that cannot be read is not one.
              // The row scrolls instead.
              flexShrink: 0, maxWidth: '260px', overflow: 'hidden',
              textOverflow: 'ellipsis'
            }
          };
        }),

        tilesStyle: (cmpReady && cmpTiles.length) ? '' : 'display:none;',
        tilesHeadStyle: cmpGrid,
        tiles: cmpTiles,

        rowsStyle: cmpReady ? '' : 'display:none;',
        rows: cmpRows,

        emptyStyle: cmpReady ? 'display:none;' : '',
        emptyText: cmpSlots.length
          ? 'One report pinned. Pin a second run from History — comparing is the ' +
            'whole mode, and one column is just the gallery again.'
          : 'Nothing pinned yet. Click a run in History above to pin it, then a ' +
            'second (and a third) — their charts line up row by row below.'
      };

      // Compare mode owns the page while it is on: the single-report gallery,
      // its tiles and its save row belong to a report that is not what is being
      // read, and leaving them under the comparison is a second answer to the
      // question the tab is currently asking.
      if (cmpOn) {
        R.viz.tileRowStyle = 'display:none;';
        R.viz.galleryStyle = 'display:none;';
        R.viz.emptyStyle = 'display:none;';
        R.viz.saveRowStyle = 'display:none;';
      }

      /* Visual is a section of its own, last in the rail. Reading results is a
       * separate sitting from driving machines, and the gallery had outgrown
       * the card it lived in on the Control tab.
       *
       * Green like Simulation on purpose -- the palette has six accents for
       * what is now seven tabs, and of the six that is the one worth
       * doubling: Simulation predicts a run, Visual measures the one that
       * happened. The rest of the style is inherited from the items the base
       * renderVals built, so the rail cannot drift into two looks. */
      var navProto = ((R.navItems || [])[0] || {}).style || {};
      R.showVisual = st.active === 'visual';
      R.navItems = (R.navItems || []).concat([{
        key: 'visual',
        label: 'Visual',
        color: 'var(--data)',
        badge: vizSaved.length ? vizSaved.length + '' : '',
        onClick: function () { self.setState({ active: 'visual', detailId: null }); },
        style: Object.assign({}, navProto, {
          background: R.showVisual ? 'var(--surface)' : 'transparent',
          color: R.showVisual ? 'var(--ink)' : 'var(--muted)',
          boxShadow: R.showVisual ? 'inset 3px 0 0 var(--data)' : 'none'
        })
      }]);

      // --- one console per target, focused + rail ---
      var outBy = st.ssh.outBy || {};
      var focus = st.ssh.focus || '';
      var server = st.ssh.server;
      var targets = [{
        id: SERVER_ID,
        name: server.user ? server.user + '@' + server.ip : 'Control server'
      }].concat(this.flatDevices().map(function (d) { return { id: d.id, name: d.name }; }));

      var focused = targets.filter(function (t) { return t.id === focus; })[0];
      if (focus && !focused) focus = '';   // device removed while focused

      R.ssh.focusName = focused ? focused.name : 'fan-out console — all targets';
      R.ssh.focusOut = focus
        ? (outBy[focus] && outBy[focus].length ? outBy[focus]
           : [{ text: '# no output from this target yet', color: '#64748b' }])
        : R.ssh.out;
      R.ssh.onClear = function () {
        self.setState(function (s) {
          var ssh = Object.assign({}, s.ssh);
          if (focus) {
            var by = Object.assign({}, ssh.outBy || {});
            delete by[focus];
            ssh.outBy = by;
          } else {
            ssh.out = [];
            ssh.outBy = {};
          }
          return { ssh: ssh };
        });
      };

      function miniStyle(active) {
        return {
          background: active ? 'var(--bg)' : '#0b1020',
          border: '1px solid ' + (active ? 'var(--edge)' : 'var(--border)'),
          borderRadius: '10px', padding: '7px 8px', cursor: 'pointer', minWidth: 0
        };
      }
      var statuses = st.ssh.status || {};
      R.ssh.miniConsoles = [{
        id: '', name: 'All targets', title: 'Everything, interleaved',
        dot: 'var(--muted)', count: (R.ssh.out || []).length + '',
        lines: (R.ssh.out || []).slice(-2),
        onClick: function () { self.sshPatch({ focus: '' }); },
        style: miniStyle(!focus)
      }].concat(targets.map(function (t) {
        var lines = outBy[t.id] || [];
        var status = statuses[t.id];
        return {
          id: t.id, name: t.name, title: t.name + ' — click to focus',
          dot: status === 'on' ? 'var(--data)' : status === 'connecting' ? 'var(--server)'
            : status === 'error' ? 'var(--alert)' : 'var(--muted)',
          count: lines.length ? lines.length + '' : '',
          lines: lines.slice(-2),
          onClick: function () { self.sshPatch({ focus: t.id }); },
          style: miniStyle(focus === t.id)
        };
      }));
      R.ssh.railStyle = 'width:190px; flex-shrink:0; display:flex; flex-direction:column;' +
        'gap:6px; max-height:392px; overflow-y:auto;';

      // --- working directory + the preset chips ---
      var cwd = st.ssh.cwd || '';
      R.ssh.cwd = cwd;
      R.ssh.onCwd = function (e) { self.sshPatch({ cwd: e.target.value }); };
      R.ssh.onEditPresets = function () { self.siEditPresets(); };

      var dirs = st.siDirs || [];
      // The row collapses entirely when empty, rather than leaving a gap that
      // reads like something failed to load.
      R.ssh.dirRowStyle = dirs.length
        ? 'display:flex; flex-wrap:wrap; gap:5px; margin-bottom:7px;'
        : 'display:none;';
      R.ssh.dirs = dirs.map(function (d) {
        var active = cwd === d.path;
        return {
          label: d.label || d.path,
          path: d.path,
          // Clicking the active chip clears it, so there is a way back to $HOME
          // without selecting the text and deleting it.
          onPick: function () { self.sshPatch({ cwd: active ? '' : d.path }); },
          style: {
            background: active ? 'var(--server)' : 'var(--bg)',
            color: active ? '#fff' : 'var(--muted)',
            border: '1px solid var(--border)', borderRadius: '7px',
            padding: '4px 9px', fontSize: '11px', fontWeight: 600,
            fontFamily: 'ui-monospace, monospace', cursor: 'pointer'
          }
        };
      });

      var presets = st.siPresets;
      if (presets && presets.length) {
        // Rebuilt rather than patched: the chip's style depends on whether it
        // is the active command, which only the render pass knows.
        R.ssh.presets = presets.map(function (p) {
          var active = st.ssh.command === p.command;
          return {
            label: p.label,
            onPick: function () { self.sshPatch({ command: p.command }); },
            style: {
              background: active ? 'var(--edge)' : 'var(--bg)',
              color: active ? '#fff' : 'var(--muted)',
              border: '1px solid var(--border)', borderRadius: '7px',
              padding: '5px 10px', fontSize: '11px', fontWeight: 600,
              fontFamily: 'ui-monospace, monospace', cursor: 'pointer'
            }
          };
        });
      }

      // --- server card: the broker block + jump toggle the build added ---
      var sv = st.ssh.server;
      R.ssh.server.amqpHost = sv.amqpHost || '';
      // Blank means "inherit BROKER_URL"; show what that resolves to rather
      // than an empty box the operator has to guess about.
      R.ssh.server.amqpHostHint = sv.amqpHostResolved || 'same as BROKER_URL';
      R.ssh.server.onAmqpHost = function (e) { self.sshServerPatch({ amqpHost: e.target.value }); };
      R.ssh.server.amqpPort = sv.amqpPort == null ? 5672 : sv.amqpPort;
      R.ssh.server.amqpUser = sv.amqpUser == null ? 'guest' : sv.amqpUser;
      R.ssh.server.amqpPassword = sv.amqpPassword || '';
      R.ssh.server.onAmqpPort = function (e) {
        self.sshServerPatch({ amqpPort: Math.max(1, Math.round(parseFloat(e.target.value) || 5672)) });
      };
      R.ssh.server.onAmqpUser = function (e) { self.sshServerPatch({ amqpUser: e.target.value }); };
      R.ssh.server.onAmqpPassword = function (e) { self.sshServerPatch({ amqpPassword: e.target.value }); };
      R.ssh.server.onToggleJump = function () { self.sshServerPatch({ jump: !sv.jump }); };
      // Collapsed, the card shows only who it is and how it is doing; the
      // fields are settings you set once and then stop reading.
      var expanded = sv.expanded !== false;   // default open on a fresh install
      R.ssh.server.expanded = expanded;
      R.ssh.server.cardCaret = expanded ? '▾' : '▸';
      R.ssh.server.cardToggleTitle = expanded ? 'Collapse' : 'Expand';
      R.ssh.server.onToggleCard = function () { self.sshServerPatch({ expanded: !expanded }); };
      R.ssh.server.subtitle = expanded
        ? 'SSH gateway · broker may be elsewhere'
        : (sv.user ? sv.user + '@' + sv.ip + ':' + (sv.port || 22) : 'not configured') +
          ' · ' + (sv.status === 'on' ? 'connected'
            : sv.status === 'connecting' ? 'connecting…'
            : sv.status === 'error' ? 'failed' : 'not connected') +
          (sv.jump ? ' · gateway' : '');

      R.ssh.server.onToggleBroker = function () { self.sshServerPatch({ showBroker: !sv.showBroker }); };
      R.ssh.server.showBroker = !!sv.showBroker;
      R.ssh.server.brokerCaret = sv.showBroker ? '▾' : '▸';
      R.ssh.server.onTestAll = function () { self.sshServerTestAll(); };
      R.ssh.server.testLabel = sv.status === 'connecting' ? 'Connecting…'
        : sv.status === 'on' ? '✓ Connected — reconnect'
        : sv.status === 'error' ? 'Retry connection'
        : 'Connect (SSH)';
      R.ssh.server.testStyle = {
        background: sv.status === 'on' ? 'var(--data)'
          : sv.status === 'error' ? 'var(--alert)' : 'var(--edge)',
        color: '#fff', border: 'none', borderRadius: '8px', padding: '8px',
        fontSize: '11px', fontWeight: 700, cursor: 'pointer'
      };
      R.ssh.server.jumpCheck = sv.jump ? '✓' : '';
      R.ssh.server.jumpBox = {
        width: '14px', height: '14px', borderRadius: '4px', display: 'flex',
        alignItems: 'center', justifyContent: 'center', fontSize: '10px', color: '#fff',
        border: '1px solid ' + (sv.jump ? 'var(--edge)' : 'var(--border)'),
        background: sv.jump ? 'var(--edge)' : 'transparent'
      };
      R.ssh.server.banner = sv.banner || '';
      R.ssh.server.bannerColor = sv.status === 'error' ? 'var(--alert)'
        : sv.status === 'partial' ? 'var(--server)' : 'var(--data)';
      // `partial` is ours -- the base renderer only knows on/connecting/off and
      // would paint this grey, reading as "never tested".
      if (sv.status === 'partial' || sv.status === 'error') {
        R.ssh.server.dot = sv.status === 'partial' ? 'var(--server)' : 'var(--alert)';
        R.ssh.server.statusLabel = sv.status === 'partial' ? 'ssh ok, see details' : 'failed';
      }

      /* The command this stage is selected in order to run.
       *
       * Matched by label against the operator's own preset list rather than
       * built from a template: those presets are editable (Control ▸ edit next
       * to the chips), so the label they chose is the only durable handle on
       * "the command that runs this stage". `run stage 1` for the first stage,
       * `run server` for the control server. The stage's name is tried first so
       * a stage called "Stage 2" finds `run stage 2` however it is ordered, and
       * its position second so the stock "Edge"/"Cloud" names still resolve.
       *
       * No such preset -- a fresh install, or a list that was renamed -- and
       * the answer is null, which leaves the command box alone. Nothing about
       * selecting targets should depend on this existing. */
      var runPreset = function (labels) {
        var list = st.siPresets || [];
        for (var i = 0; i < labels.length; i++) {
          for (var j = 0; j < list.length; j++) {
            if (normLabel(list[j].label) === normLabel(labels[i])) return list[j];
          }
        }
        return null;
      };

      // --- the group's own select-all, wired for the markup the build adds ---
      // "clear" only once the whole group is ticked; a group of one says
      // "select", because "select all" of a single row reads like a mistake.
      var groupSelect = function (devices, labels) {
        var ids = (devices || []).map(function (d) { return d.id; });
        var sel = st.ssh.selected || [];
        var all = ids.length > 0 && ids.every(function (id) { return sel.indexOf(id) >= 0; });
        var preset = all ? null : runPreset(labels || []);
        return {
          selectAllLabel: all ? 'clear' : ids.length > 1 ? 'select all' : 'select',
          selectAllTitle: all
            ? 'Unselect every target in this stage'
            : 'Select every target in this stage — other stages keep their selection' +
              (preset ? ', and load “' + preset.label + '”' : ''),
          onSelectAll: function () { self.siSelectGroup(ids, preset); }
        };
      };

      // --- the control server as a selectable target ---
      // Prepended as its own group so it reads as infrastructure rather than
      // as another inference device.
      var serverOn = (st.ssh.selected || []).indexOf(SERVER_ID) >= 0;
      var serverStatus = (st.ssh.status || {})[SERVER_ID];
      R.ssh.groups = [Object.assign({
        name: 'Control server' + (sv.jump ? ' · gateway' : ''),
        color: 'var(--server)',
        devices: [{
          id: SERVER_ID,
          name: sv.user ? sv.user + '@' + sv.ip : 'not configured',
          host: 'ssh :' + (sv.port || 22) + (sv.jump ? ' · jump host' : ''),
          cluster: '', clusterLabel: '', ip: sv.ip, port: sv.port || 22, user: sv.user, password: '',
          editing: false,
          onToggle: function () { self.sshToggle(SERVER_ID); },
          // Its settings live in the card above, so the row's own ⚙ form would
          // be a second, diverging place to edit them.
          onExpand: function (e) { e.stopPropagation(); self.setState({ active: 'control' }); },
          onIp: function () {}, onPort: function () {}, onUser: function () {}, onPassword: function () {},
          check: serverOn ? '✓' : '',
          checkColor: serverOn ? 'var(--server)' : 'var(--border)',
          checkBg: serverOn ? 'var(--server)' : 'transparent',
          dot: serverStatus === 'on' ? 'var(--data)'
            : serverStatus === 'connecting' ? 'var(--server)'
            : serverStatus === 'error' ? 'var(--alert)' : 'var(--muted)',
          statusLabel: serverStatus === 'on' ? 'connected'
            : serverStatus === 'connecting' ? 'connecting…'
            : serverStatus === 'error' ? 'error' : 'offline',
          wrapStyle: { borderRadius: '9px', border: '1px solid transparent', background: 'transparent' },
          rowStyle: {
            display: 'flex', alignItems: 'center', gap: '9px', padding: '7px 8px',
            borderRadius: '9px', cursor: 'pointer',
            border: '1px solid ' + (serverOn ? 'var(--server)' : 'var(--border)'),
            background: serverOn ? 'var(--bg)' : 'transparent'
          }
        }]
      }, groupSelect([{ id: SERVER_ID }], ['run server']))].concat((R.ssh.groups || []).map(function (g, i) {
        // The build replaced the row's hardcoded "c<n>" with a field, so every
        // real device has to supply its own now; and the ⚙ form gained a
        // Connect button that needs wiring.
        return Object.assign({}, g, groupSelect(g.devices, [
          'run ' + g.name, 'run stage ' + (i + 1)
        ]), {
          devices: (g.devices || []).map(function (d) {
            var status = (st.ssh.status || {})[d.id];
            var clip = st.ssh.clip;
            var smallButton = function (enabled, accent) {
              return {
                flex: 1, border: '1px solid var(--border)', borderRadius: '7px',
                padding: '5px', fontSize: '10px', fontWeight: 600,
                cursor: enabled ? 'pointer' : 'not-allowed',
                background: 'var(--surface)',
                color: enabled ? (accent || 'var(--ink)') : 'var(--muted)'
              };
            };
            return Object.assign({}, d, {
              clusterLabel: 'c' + d.cluster,
              onCopy: function () { self.siCopyDevice(d.id); },
              copyStyle: smallButton(true),
              onPaste: function () { self.siPasteDevice(d.id); },
              pasteLabel: clip ? '⎗ paste ' + clip.from : '⎗ paste login',
              pasteTitle: clip
                ? 'Apply ' + clip.from + "'s port/username/password here (Ctrl+V). The IP is left alone."
                : 'Copy a device’s login first',
              pasteStyle: smallButton(!!clip, 'var(--edge)'),
              onConnect: function () { self.siConnectDevice(d.id); },
              connectLabel: status === 'connecting' ? 'Connecting…'
                : status === 'on' ? '✓ Connected — reconnect'
                : status === 'error' ? 'Retry connection'
                : 'Connect',
              connectStyle: {
                gridColumn: '1 / -1', marginTop: '2px', border: 'none',
                borderRadius: '7px', padding: '7px', fontSize: '11px',
                fontWeight: 700, cursor: 'pointer', color: '#fff',
                background: status === 'on' ? 'var(--data)'
                  : status === 'error' ? 'var(--alert)' : 'var(--edge)'
              }
            });
          })
        });
      }));

      if (!live) return R;

      // ---- live-only refinements -------------------------------------
      var clusters = this.buildClusters();
      var raw = st.siRaw || {};

      // Real queue depth instead of the ratio-derived stand-in.
      (R.pipelineNodes || []).forEach(function (node) {
        if (!node.isQueue) return;
        node.queues = (node.queues || []).map(function (q, i) {
          var p = raw[(clusters[i] || {}).id];
          if (!p || p.queue_depth == null) return q;
          var depth = Math.min(8, p.queue_depth);
          var slots = [];
          for (var k = 0; k < 8; k++) slots.push({ fill: k < depth ? 'var(--broker)' : 'var(--border)' });
          return Object.assign({}, q, { slots: slots });
        });
      });

      // Per-device utilization: measured per device rather than shared from
      // the cluster's stage average.
      var utilById = {};
      Object.keys(raw).forEach(function (k) {
        ((raw[k] || {}).devices || []).forEach(function (d) {
          if (d && d.id != null && d.util != null) utilById[d.id] = d.util;
        });
      });
      if (Object.keys(utilById).length) {
        var byName = {};
        this.flatDevices().forEach(function (d) {
          if (utilById[d.id] != null) byName[d.name] = utilById[d.id];
        });
        R.deviceUtil = (R.deviceUtil || []).map(function (row) {
          if (byName[row.name] == null) return row;
          var v = byName[row.name];
          return Object.assign({}, row, {
            v: v, pct: self.f0(v * 100), w: Math.min(100, v * 100).toFixed(1) + '%'
          });
        });
      }

      // Say where the numbers came from, so live and simulated are never
      // confused on screen.
      var sources = Object.keys(raw).map(function (k) { return (raw[k] || {}).source; });
      var measured = sources.filter(function (s) { return s === 'live'; }).length;
      R.simTiles = (R.simTiles || []).concat([{
        label: 'Source', value: measured ? 'Live' : 'Server sim',
        unit: measured ? measured + ' cluster(s) measured' : 'no run active',
        color: measured ? 'var(--data)' : 'var(--muted)'
      }]);

      return R;
    }
  });
})();
