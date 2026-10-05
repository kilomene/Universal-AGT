/* Universal AGT control-plane dashboard.
 *
 * Static page (no build step, no framework). It is served as static files by
 * the control plane at ANY base path, so everything — assets and API calls —
 * uses relative URLs.
 *
 * Auth: the API key is kept in sessionStorage only (cleared when the tab
 * closes) and is sent as `Authorization: Bearer <key>` on every fetch.
 * EventSource cannot set request headers, so the live stream at
 * GET /v1/events/stream carries the key as `?api_key=` INSTEAD — that is a
 * deliberate tradeoff (query params can show up in access logs), and the
 * control-plane API team was told to accept that parameter on the stream
 * endpoint. If the API rejects it, events will just fail closed with the
 * banner below; panels keep polling.
 */
(function () {
  'use strict';

  // Relative API root — resolves against the page's own URL, so this works
  // no matter which base path the control plane serves the dashboard from.
  var API = 'v1';
  var KEY_STORE = 'uagt_api_key';
  var REFRESH_MS = 10000;
  var MAX_EVENTS = 200;
  var ONLINE_WINDOW_S = 90; // heartbeat freshness used to derive host status

  var state = {
    key: sessionStorage.getItem(KEY_STORE) || '',
    source: null,
    refreshTimer: null,
    hardReconnectAt: 0
  };

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function short(id) {
    if (!id) return '&mdash;';
    var s = String(id);
    return esc(s.length > 12 ? s.slice(0, 8) + '\u2026' : s);
  }

  function fmtTime(iso) {
    if (!iso) return '\u2014';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '\u2014';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function timeAgo(iso) {
    if (!iso) return 'never';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return 'never';
    var s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (s < 60) return s + 's ago';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function setConn(mode, detail) {
    var c = el('conn');
    c.classList.remove('ok', 'bad', 'warn', 'unknown');
    if (mode === 'live') {
      c.classList.add('ok');
      c.textContent = 'live';
    } else if (mode === 'error') {
      c.classList.add('bad');
      c.textContent = 'error' + (detail ? ': ' + detail : '');
    } else if (mode === 'reconnecting') {
      c.classList.add('warn');
      c.textContent = 'reconnecting\u2026';
    } else {
      c.classList.add('unknown');
      c.textContent = 'connecting\u2026';
    }
  }

  function authHeaders() {
    return { 'Authorization': 'Bearer ' + state.key, 'Accept': 'application/json' };
  }

  /* ---------------- auth gate ---------------- */

  function showAuth(message) {
    el('authOverlay').hidden = false;
    el('logoutBtn').hidden = true;
    var err = el('authError');
    if (message) { err.textContent = message; err.hidden = false; }
    else { err.hidden = true; }
    setTimeout(function () { el('apiKeyInput').focus(); }, 0);
  }

  function hideAuth() {
    el('authOverlay').hidden = true;
    el('logoutBtn').hidden = false;
  }

  function requireAuth(message) {
    stopAll();
    showAuth(message || 'Your session key was rejected. Sign in again.');
  }

  /* ---------------- API ---------------- */

  function api(path) {
    return fetch(API + path, { headers: authHeaders() }).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        requireAuth('That API key was rejected (HTTP ' + res.status + ').');
        throw new Error('unauthorized');
      }
      if (!res.ok) {
        throw new Error('HTTP ' + res.status + ' on ' + path);
      }
      if (res.status === 204) return null;
      return res.json();
    });
  }

  function panelError(panelId, err) {
    var p = el(panelId + 'Error');
    p.textContent = 'Failed to load: ' + err.message;
    p.hidden = false;
  }

  function panelOk(panelId) {
    el(panelId + 'Error').hidden = true;
    el(panelId + 'Updated').textContent = 'updated ' + fmtTime(new Date().toISOString());
  }

  /* ---------------- shared widgets ---------------- */

  var STATUS_CLASS = {
    queued: 'st-queued', claimed: 'st-claimed', running: 'st-running',
    completed: 'st-ok', failed: 'st-bad', cancelled: 'st-muted',
    retrying: 'st-running', awaiting_approval: 'st-wait',
    requested: 'st-queued', approved: 'st-claimed', approved_manual: 'st-claimed',
    building: 'st-running', starting: 'st-running', healthcheck: 'st-running',
    stopping: 'st-warn', stopped: 'st-muted', rolled_back: 'st-warn',
    healthy: 'st-ok', unhealthy: 'st-bad', passing: 'st-ok', failing: 'st-bad',
    online: 'st-ok', offline: 'st-muted', unknown: 'st-muted'
  };

  function badge(status) {
    var cls = STATUS_CLASS[String(status || 'unknown').toLowerCase()] || 'st-muted';
    return '<span class="badge ' + cls + '">' + esc(status || 'unknown') + '</span>';
  }

  function bar(pct) {
    var v = Math.max(0, Math.min(100, Number(pct) || 0));
    var heat = v >= 90 ? 'hot' : (v >= 70 ? 'warm' : '');
    return '<span class="bar"><span class="track"><span class="fill ' + heat +
      '" style="width:' + v.toFixed(0) + '%"></span></span>' +
      '<span class="val">' + v.toFixed(0) + '%</span></span>';
  }

  function emptyRow(cols, label) {
    return '<tr class="empty"><td colspan="' + cols + '">' + esc(label) + '</td></tr>';
  }

  /* ---------------- panel renderers ---------------- */

  function hostStatus(host) {
    if (host.status) return host.status;
    var last = host.last_seen || host.last_heartbeat_at;
    if (!last) return 'unknown';
    var ageS = (Date.now() - new Date(last).getTime()) / 1000;
    return ageS <= ONLINE_WINDOW_S ? 'online' : 'offline';
  }

  function renderHosts(body) {
    var hosts = body && (body.hosts || body.items || body.data) || [];
    if (!hosts.length) {
      el('hostsBody').innerHTML = emptyRow(8, 'No hosts registered yet.');
      return;
    }
    el('hostsBody').innerHTML = hosts.map(function (h) {
      var apps = h.running_apps || h.apps || [];
      var cpu = h.cpu_pct != null ? h.cpu_pct : h.cpu;
      var ram = h.ram_pct != null ? h.ram_pct : h.ram;
      var disk = h.disk_pct != null ? h.disk_pct : h.disk;
      return '<tr>' +
        '<td><span class="mono">' + esc(h.name || h.host_name || short(h.id)) + '</span></td>' +
        '<td>' + badge(hostStatus(h)) + '</td>' +
        '<td>' + bar(cpu) + '</td>' +
        '<td>' + bar(ram) + '</td>' +
        '<td>' + bar(disk) + '</td>' +
        '<td class="mono">' + esc(apps.length) + '</td>' +
        '<td class="mono">' + esc(h.worker_version || h.version || '\u2014') + '</td>' +
        '<td>' + esc(timeAgo(h.last_seen || h.last_heartbeat_at)) + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderServices(body) {
    var svcs = body && (body.services || body.items || body.data) || [];
    if (!svcs.length) {
      el('servicesBody').innerHTML = emptyRow(7, 'No running services.');
      return;
    }
    el('servicesBody').innerHTML = svcs.map(function (s) {
      var ports = s.ports || {};
      var portList = Object.keys(ports).map(function (k) {
        return esc(k + ':' + ports[k]);
      }).join(', ') || '\u2014';
      var domains = (s.domains || []).map(esc).join(', ') || '\u2014';
      return '<tr>' +
        '<td><span class="mono">' + esc(s.name || s.service_name || short(s.id)) + '</span></td>' +
        '<td class="mono">' + esc(s.version || '\u2014') + '</td>' +
        '<td>' + badge(s.status || 'running') + '</td>' +
        '<td>' + badge(s.health_status || s.health || 'unknown') + '</td>' +
        '<td><span class="mono">' + esc(s.host_name || short(s.host_id)) + '</span></td>' +
        '<td class="mono">' + portList + '</td>' +
        '<td class="mono">' + domains + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderDeployments(body) {
    var deps = body && (body.deployments || body.items || body.data) || [];
    if (!deps.length) {
      el('deploymentsBody').innerHTML = emptyRow(6, 'No deployments.');
      return;
    }
    el('deploymentsBody').innerHTML = deps.map(function (d) {
      var dep = d.deployment || d;
      return '<tr>' +
        '<td class="mono">' + short(dep.id) + '</td>' +
        '<td class="mono">' + esc(dep.version || '\u2014') + '</td>' +
        '<td>' + badge(dep.status) + '</td>' +
        '<td>' + badge(dep.health_status || 'unknown') + '</td>' +
        '<td><span class="mono">' + esc(dep.host_name || short(dep.host_id)) + '</span></td>' +
        '<td>' + esc(timeAgo(dep.created_at)) + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderTasks(body) {
    var tasks = body && (body.tasks || body.items || body.data) || [];
    if (!tasks.length) {
      el('tasksBody').innerHTML = emptyRow(6, 'No tasks.');
      return;
    }
    el('tasksBody').innerHTML = tasks.map(function (t) {
      return '<tr>' +
        '<td class="mono">' + short(t.id) + '</td>' +
        '<td class="mono">' + esc(t.type || '\u2014') + '</td>' +
        '<td>' + badge(t.status) + '</td>' +
        '<td><span class="mono">' + esc(t.claimed_by ? short(t.claimed_by) : (t.assigned_to ? short(t.assigned_to) : '\u2014')) + '</span></td>' +
        '<td class="mono">' + esc(t.attempts != null ? t.attempts : 0) + '</td>' +
        '<td>' + esc(timeAgo(t.created_at)) + '</td>' +
        '</tr>';
    }).join('');
  }

  /* ---------------- events ---------------- */

  function eventDetail(ev) {
    var bits = [];
    if (ev.actor_type || ev.actor_id) bits.push('actor=' + (ev.actor_type || '?') + ':' + (ev.actor_id || '?'));
    if (ev.task_id) bits.push('task=' + String(ev.task_id).slice(0, 8));
    if (ev.deployment_id) bits.push('deployment=' + String(ev.deployment_id).slice(0, 8));
    if (ev.host_id) bits.push('host=' + String(ev.host_id).slice(0, 8));
    if (ev.payload) {
      if (ev.payload.message) bits.push(ev.payload.message);
      else if (ev.payload.error) bits.push('error: ' + ev.payload.error);
    }
    return bits.join('  ');
  }

  function prependEvent(ev) {
    if (!ev || !ev.type) return;
    var list = el('eventsList');
    var placeholder = list.querySelector('.empty-events');
    if (placeholder) placeholder.remove();
    var li = document.createElement('li');
    var time = document.createElement('span');
    time.className = 't';
    time.textContent = fmtTime(ev.created_at);
    var typeBadge = document.createElement('span');
    typeBadge.innerHTML = badge(ev.type);
    var detail = document.createElement('span');
    detail.className = 'detail';
    detail.textContent = eventDetail(ev);
    detail.title = JSON.stringify(ev);
    li.appendChild(time);
    li.appendChild(typeBadge);
    li.appendChild(detail);
    list.insertBefore(li, list.firstChild);
    while (list.children.length > MAX_EVENTS) {
      list.removeChild(list.lastChild);
    }
    el('eventsUpdated').textContent = 'streaming \u00b7 ' + fmtTime(new Date().toISOString());
  }

  function connectStream() {
    if (state.source) {
      try { state.source.close(); } catch (e) { /* already closed */ }
      state.source = null;
    }
    if (!state.key) return;
    // EventSource cannot set request headers, so the API key travels as a
    // query parameter on the stream URL ONLY (tradeoff: may appear in server
    // access logs). The control-plane API was told to accept `api_key` on
    // GET /v1/events/stream. Everywhere else this page uses
    // `Authorization: Bearer`.
    var url = API + '/events/stream?api_key=' + encodeURIComponent(state.key);
    var src = new EventSource(url);
    state.source = src;
    state.hardReconnectAt = Date.now() + 60000;

    src.onopen = function () {
      setConn('live');
      hideStreamError();
      state.hardReconnectAt = Date.now() + 60000;
    };

    src.onmessage = function (msg) {
      try {
        prependEvent(JSON.parse(msg.data));
      } catch (e) {
        // non-JSON frames (e.g. SSE comments / keepalive pings) are ignored
      }
    };

    src.onerror = function () {
      // EventSource reconnects automatically with backoff. If it stays
      // broken for a full minute, force a fresh connection ourselves.
      setConn('reconnecting');
      if (Date.now() > state.hardReconnectAt) {
        showStreamError('Event stream unreachable — retrying with a fresh connection.');
        connectStream();
      }
    };
  }

  function showStreamError(msg) {
    var p = el('eventsError');
    p.textContent = msg;
    p.hidden = false;
  }

  function hideStreamError() { el('eventsError').hidden = true; }

  /* ---------------- refresh loop ---------------- */

  var PANELS = [
    { id: 'hosts',       path: '/hosts',            render: renderHosts },
    { id: 'services',    path: '/services',         render: renderServices },
    { id: 'deployments', path: '/deployments?limit=20', render: renderDeployments },
    { id: 'tasks',       path: '/tasks?limit=20',   render: renderTasks }
  ];

  function refreshAll() {
    if (!state.key) return;
    var anyOk = false;
    var jobs = PANELS.map(function (p) {
      return api(p.path).then(function (body) {
        p.render(body);
        panelOk(p.id);
        anyOk = true;
      }).catch(function (err) {
        if (err && err.message === 'unauthorized') return; // requireAuth already ran
        panelError(p.id, err);
      });
    });
    // Identify the signed-in agent once per page load.
    jobs.push(
      api('/agents/me').then(function (body) {
        var agent = (body && body.agent) || body || {};
        var label = agent.name ? 'signed in as ' + agent.name : '';
        el('agentName').textContent = label;
      }).catch(function () { /* non-fatal */ })
    );
    Promise.all(jobs).then(function () {
      if (anyOk && el('conn').classList.contains('unknown')) setConn('live');
    });
  }

  function startLoops() {
    stopAll();
    connectStream();
    refreshAll();
    state.refreshTimer = setInterval(refreshAll, REFRESH_MS);
  }

  function stopAll() {
    if (state.refreshTimer) { clearInterval(state.refreshTimer); state.refreshTimer = null; }
    if (state.source) {
      try { state.source.close(); } catch (e) { /* already closed */ }
      state.source = null;
    }
  }

  /* ---------------- wiring ---------------- */

  el('authForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var val = el('apiKeyInput').value.trim();
    if (!val) return;
    state.key = val;
    sessionStorage.setItem(KEY_STORE, val);
    el('apiKeyInput').value = '';
    hideAuth();
    setConn('connecting');
    startLoops();
  });

  el('logoutBtn').addEventListener('click', function () {
    state.key = '';
    sessionStorage.removeItem(KEY_STORE);
    stopAll();
    setConn('connecting');
    showAuth();
  });

  // Boot: key already in this tab's session storage? Go straight in.
  if (state.key) {
    hideAuth();
    startLoops();
  } else {
    showAuth();
  }
})();
