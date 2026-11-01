(function () {
  var CFG = window.LXCW, I18N = CFG.i18n;  // set by the page (see index.html)
  function fmt(template, values) {
    return template.replace(/\{(\w+)\}/g, function (_, key) { return key in values ? values[key] : '{' + key + '}'; });
  }
  var btn = document.getElementById('refresh'), sub = document.getElementById('sub');
  var lastCheck = CFG.lastCheck, timer = null;
  var jobId = CFG.jobId, jobStatus = CFG.jobStatus;

  function waiting() {
    btn.disabled = true;
    btn.textContent = I18N.checking;
    btn.classList.add('spin');
    sub.textContent = I18N.check_starts;
    watch();
    startProgress();
  }
  function watch() { if (!timer) timer = setInterval(poll, 5000); }
  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
      if (s.job_id !== jobId || s.job !== jobStatus || (!s.refreshing && s.checked_at !== lastCheck)) reloadKeepingConsole();
    }).catch(function () {});
  }

  var msg = document.getElementById('msg');
  function act(action, extra) {
    msg.textContent = '';
    fetch('/api/actions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign(Array.isArray(action) ? { actions: action } : { action: action }, extra || {}))
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error((body.code && I18N.api_err[body.code]) || body.error || I18N.request_refused);
        location.reload();
      });
    }).catch(function (e) { msg.textContent = e.message; });
  }

  var pve = document.getElementById('act-pve');
  if (pve) pve.addEventListener('click', function () { act('update_pve_scripts'); });
  var backup = document.getElementById('opt-backup');
  if (backup) {  // the choice is remembered by this browser
    try { var savedBackup = localStorage.getItem('lxcw_backup'); if (savedBackup !== null) backup.checked = savedBackup === '1'; } catch (e) {}
    backup.addEventListener('change', function () {
      try { localStorage.setItem('lxcw_backup', backup.checked ? '1' : '0'); } catch (e) {}
    });
  }
  var cleanup = document.getElementById('opt-cleanup');
  if (cleanup) {
    try { var savedCleanup = localStorage.getItem('lxcw_cleanup'); if (savedCleanup !== null) cleanup.checked = savedCleanup === '1'; } catch (e) {}
    cleanup.addEventListener('change', function () {
      try { localStorage.setItem('lxcw_cleanup', cleanup.checked ? '1' : '0'); } catch (e) {}
    });
  }
  var picker = document.getElementById('act-picker'), run = document.getElementById('act-run');
  if (picker && run) {
    var boxes = [].slice.call(picker.querySelectorAll('input[type=checkbox]')), summary = document.getElementById('act-summary');
    var placeholder = summary.textContent;
    var chosen = function () { return boxes.filter(function (b) { return b.checked && !b.disabled; }); };
    var refreshPicker = function () {
      var list = chosen();
      summary.textContent = list.length ? list.map(function (b) { return b.dataset.label; }).join(', ') : placeholder;
      run.disabled = !list.length;
    };
    boxes.forEach(function (b) { b.addEventListener('change', refreshPicker); });
    document.addEventListener('click', function (e) { if (!picker.contains(e.target)) picker.open = false; });
    run.addEventListener('click', function () {
      var list = chosen();
      if (!list.length) return;
      picker.open = false;
      var values = list.map(function (b) { return b.value; });
      var withBackup = !!(backup && backup.checked) && values.some(function (v) { return v !== 'upgrade_os'; });
      var withCleanup = !!(cleanup && cleanup.checked) && values.indexOf('update_docker') >= 0;
      act(values.length === 1 ? values[0] : values, { backup: withBackup, cleanup: withCleanup });
    });
    refreshPicker();
  }

  // Live console: the collector streams the job output to the service and the page polls it. A line starting with
  // "[CT n]" belongs to container n: the tabs filter by container and show where each container stands.
  var consoleEl = document.getElementById('console'), tabsEl = document.getElementById('console-tabs');
  var logOffset = 0, style = { fg: '', bold: false }, partial = '', activeCt = '', ctState = {}, ctColor = {};
  var tabs = tabsEl ? [].slice.call(tabsEl.querySelectorAll('button[data-ct]')) : [];
  tabs.forEach(function (tab, i) {
    if (tab.dataset.ct) { ctState[tab.dataset.ct] = 'waiting'; ctColor[tab.dataset.ct] = 'k' + ((i - 1) % 6); }
    tab.addEventListener('click', function () { selectTab(tab.dataset.ct); });
  });
  function selectTab(ct) {
    activeCt = ct;
    tabs.forEach(function (tab) {
      var on = tab.dataset.ct === ct;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    [].forEach.call(consoleEl.children, function (line) { line.hidden = ct !== '' && line.dataset.ct !== ct; });
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
  function setState(ct, state) {
    if (!(ct in ctState) || ctState[ct] === 'ok' || ctState[ct] === 'failed') return;
    ctState[ct] = state;
    var tab = tabs.filter(function (t) { return t.dataset.ct === ct; })[0];
    if (tab) tab.querySelector('.dot').className = 'dot ' + state;
  }
  function appendStyled(parent, text) {
    var parts = text.replace(/\x1b\[[0-9;?]*[A-Za-ln-z]/g, '').split(/(\x1b\[[0-9;]*m)/);
    parts.forEach(function (part) {
      var sgr = /^\x1b\[([0-9;]*)m$/.exec(part);
      if (sgr) {
        (sgr[1] || '0').split(';').forEach(function (code) {
          var c = +code;
          if (c === 0) { style.fg = ''; style.bold = false; }
          else if (c === 1) style.bold = true;
          else if (c === 22) style.bold = false;
          else if (c === 39) style.fg = '';
          else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) style.fg = 'c' + c;
        });
      } else if (part) {
        var span = document.createElement('span');
        span.textContent = part;
        span.className = (style.fg + (style.bold ? ' b1' : '')).trim();
        parent.appendChild(span);
      }
    });
  }
  function addLine(text) {
    var line = document.createElement('div'), tag = /^\[CT (\d+)\] /.exec(text), ct = tag ? tag[1] : '', rest = text;
    line.className = 'ln';
    line.dataset.ct = ct;
    if (tag) {
      var label = document.createElement('span');
      label.className = 'ct-tag ' + (ctColor[ct] || 'k0');
      label.textContent = tag[0];
      line.appendChild(label);
      rest = text.slice(tag[0].length);
      var done = /== finished: (ok|failed) ==/.exec(rest.replace(/\x1b\[[0-9;]*m/g, ''));
      setState(ct, done ? done[1] : 'running');
    }
    appendStyled(line, rest);
    line.hidden = activeCt !== '' && ct !== activeCt;
    consoleEl.appendChild(line);
  }
  function pullLog() {
    if (!consoleEl || !jobId) return;
    fetch('/api/jobs/' + jobId + '/log?offset=' + logOffset).then(function (r) { return r.json(); }).then(function (d) {
      var nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
      var lines = (partial + (d.text || '')).split('\n');
      partial = lines.pop();  // the last line may be incomplete: it is completed by the next poll
      lines.forEach(function (text) { if (text) addLine(text); });
      logOffset = d.offset;
      if (d.status === 'done') {
        if (partial) { addLine(partial); partial = ''; }
        Object.keys(ctState).forEach(function (ct) { setState(ct, 'failed'); });  // never finished: interrupted
      }
      if (lines.length && nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
    }).catch(function () {});
  }
  // The page reloads itself when a job or a check ends: keep the console open on the same tab for the same job.
  function reloadKeepingConsole() {
    try {
      var box = document.getElementById('console-box');
      if (box && jobId) sessionStorage.setItem('lxcw_console', JSON.stringify({ job: jobId, open: box.open, ct: activeCt }));
    } catch (e) {}
    location.reload();
  }
  try {
    var savedConsole = JSON.parse(sessionStorage.getItem('lxcw_console') || 'null');
    sessionStorage.removeItem('lxcw_console');
    if (savedConsole && savedConsole.job === jobId && consoleEl) {
      document.getElementById('console-box').open = savedConsole.open;
      if (savedConsole.ct && tabs.length) selectTab(savedConsole.ct);
    }
  } catch (e) {}
  pullLog();
  if (CFG.jobActive) {
    setInterval(pullLog, 2000);
    watch();
  }

  // Live check: the collector reports every step of every container and each row animates as the results arrive.
  var progressBox = document.getElementById('progress'), progressFill = progressBox.querySelector('.fill');
  var progressText = document.getElementById('progress-text'), progressTimer = null, rowSeen = {};
  function badgeNode(item) {
    var node = document.createElement('span');
    node.className = 'b ' + item.kind + ' pop';
    node.textContent = item.text;
    node.title = item.text;
    return node;
  }
  function checkingNode(label) {
    var node = document.createElement('span');
    node.className = 'checking';
    node.textContent = label;
    return node;
  }
  function paint(cell, nodes) { cell.replaceChildren.apply(cell, nodes); }
  function applyProgress(d) {
    progressBox.hidden = false;
    progressFill.style.width = (d.total ? Math.round(100 * d.done / d.total) : 0) + '%';
    progressText.textContent = fmt(I18N.live_progress, { done: d.done, total: d.total });
    Object.keys(d.containers).forEach(function (ctid) {
      var c = d.containers[ctid], row = document.querySelector('[data-ctid="' + ctid + '"]');
      if (!row || c.status !== 'running') return;
      var sysCell = row.querySelector('.sys-cell'), appCell = row.querySelector('.app-cell');
      var seen = rowSeen[ctid] || (rowSeen[ctid] = {});
      row.classList.toggle('is-queued', c.steps.system === 'queued');
      if (c.steps.system === 'running' && seen.system !== 'running') paint(sysCell, [checkingNode(I18N.step_system)]);
      if (c.steps.system === 'done' && seen.system !== 'done' && c.sys) paint(sysCell, [badgeNode(c.sys)]);
      seen.system = c.steps.system;
      if (c.steps.app !== 'queued') {
        var appKey = c.steps.app + '/' + c.steps.docker + '/' + JSON.stringify(c.apps || []);
        if (seen.app !== appKey) {
          var nodes = (c.apps || []).map(badgeNode);
          if (c.steps.app === 'running') nodes.push(checkingNode(I18N.step_app));
          else if (c.steps.docker === 'running') nodes.push(checkingNode(I18N.step_docker));
          if (nodes.length) paint(appCell, nodes);
          seen.app = appKey;
        }
      }
      var finished = c.steps.system === 'done' && c.steps.app === 'done' && c.steps.docker === 'done';
      if (finished && c.pending && !seen.flashed) { row.classList.add('flash'); seen.flashed = true; }
    });
  }
  function pullProgress() {
    fetch('/api/progress').then(function (r) { return r.json(); }).then(function (d) {
      if (d.checked_at !== lastCheck) {  // the report is complete: show the final, re-sorted page
        clearInterval(progressTimer);
        setTimeout(reloadKeepingConsole, 900);
      } else if (d.active) {
        startProgress();
        applyProgress(d);
      }
    }).catch(function () {});
  }
  function startProgress() { if (!progressTimer) { progressTimer = setInterval(pullProgress, 1000); pullProgress(); } }
  pullProgress();  // a collection may already be running (daily timer, or started from another tab)

  btn.addEventListener('click', function () {
    btn.disabled = true;
    fetch('/api/refresh', { method: 'POST' }).then(function (r) {
      if (!r.ok) throw new Error();
      waiting();
    }).catch(function () { btn.disabled = false; sub.textContent = I18N.request_failed; });
  });
  if (CFG.refreshing) waiting();
})();
