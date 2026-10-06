/* Universal AGT control-plane dashboard.
 *
 * Static page (no build step, no framework). It is served as static files by
 * the control plane at ANY base path, so everything — assets and API calls —
 * uses relative URLs.
 *
 * Auth: the API key is kept in sessionStorage only (cleared when the tab
 * closes) and is sent as `Authorization: Bearer <key>` on every fetch,
 * INCLUDING mutating calls (never a query param). A 401 re-arms the auth
 * gate; a 403 means the key is valid but lacks the permission — the panel or
 * action shows "not permitted" instead of signing the user out.
 *
 * EventSource cannot set request headers, so the live stream at
 * GET /v1/events/stream carries the key as `?api_key=` INSTEAD — that is a
 * deliberate tradeoff (query params can show up in access logs), and the
 * control-plane API team was told to accept that parameter on the stream
 * endpoint. If the API rejects it, events will just fail closed with the
 * banner below; panels keep polling.
 *
 * NOTE: there is no GET /v1/agents list endpoint in the control-plane API
 * (only /agents/register, /agents/me, /agents/me/rotate), so the dashboard
 * has no agents table. "Agents active" in the overview strip is derived from
 * recent events (actor_type='agent' seen in the last 15 minutes).
 */
(function () {
  'use strict';

  // Relative API root — resolves against the page's own URL, so this works
  // no matter which base path the control plane serves the dashboard from.
  var API = 'v1';
  var KEY_STORE = 'uagt_api_key';
  var REFRESH_MS = 10000;
  var MAX_EVENTS = 200;
  var ONLINE_WINDOW_S = 90; // heartbeat freshness fallback when host.status is absent
  var AGENT_ACTIVE_WINDOW_MS = 15 * 60 * 1000;

  var state = {
    key: sessionStorage.getItem(KEY_STORE) || '',
    source: null,
    refreshTimer: null,
    hardReconnectAt: 0,
    cache: {},          // panel id -> last response body (drives the overview strip)
    hostsById: {},      // host id -> host row (for name lookups in other panels)
    approvalDetail: {}, // task id -> Promise<dossier> (fetched once per task)
    expanded: {},       // task id -> true while its dossier row is open
    pendingApprove: null, // task id currently staged in the approve modal
    logsTimer: null     // pending setTimeout id for the logs panel poll
  };

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function short(id) {
    // Returns the literal em dash for missing ids (NOT the &mdash; entity)
    // so callers can safely wrap the result in esc() without double-escaping.
    if (!id) return '\u2014';
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

  function fmtBytes(n) {
    if (n == null || isNaN(Number(n))) return '\u2014';
    var v = Number(n);
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
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

  function forbiddenError(path, res) {
    var e = new Error('not permitted (HTTP 403 on ' + path + ') — this key lacks the permission');
    e.code = 'forbidden';
    e.status = 403;
    return e;
  }

  // Read call (GET). 401 re-arms the auth gate; 403 is a per-panel error.
  function api(path) {
    return fetch(API + path, { headers: authHeaders() }).then(function (res) {
      if (res.status === 401) {
        requireAuth('That API key was rejected (HTTP 401).');
        throw new Error('unauthorized');
      }
      if (res.status === 403) throw forbiddenError(path, res);
      if (!res.ok) {
        throw new Error('HTTP ' + res.status + ' on ' + path);
      }
      if (res.status === 204) return null;
      return res.json();
    });
  }

  // Mutating call (POST/PUT/DELETE). Bearer <redacted> header, never a query
  // param. 401 re-arms the auth gate; 403 surfaces as `forbidden` so the
  // caller can show "not permitted" next to the action. `body`, when given,
  // is sent as JSON (e.g. creating a logs task).
  function apiWrite(method, path, body) {
    var opts = { method: method, headers: authHeaders() };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(API + path, opts).then(function (res) {
      if (res.status === 401) {
        requireAuth('That API key was rejected (HTTP 401).');
        throw new Error('unauthorized');
      }
      if (res.status === 403) throw forbiddenError(path, res);
      if (!res.ok) {
        var e = new Error('HTTP ' + res.status + ' on ' + path);
        e.status = res.status;
        throw e;
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
    online: 'st-ok', offline: 'st-muted', unknown: 'st-muted',
    degraded: 'st-warn', draining: 'st-warn'
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

  function rowsOf(body, keys) {
    if (!body) return [];
    for (var i = 0; i < keys.length; i++) {
      if (Array.isArray(body[keys[i]])) return body[keys[i]];
    }
    return [];
  }

  /* ---------------- panel renderers ---------------- */

  // Server-side truth first: the control plane's stale-host sweeper maintains
  // host.status (online|degraded|offline|draining). Client-side heartbeat
  // freshness is only a fallback when the field is absent (older API).
  function hostStatus(host) {
    if (host.status) return host.status;
    var last = host.last_seen || host.last_heartbeat_at;
    if (!last) return 'unknown';
    var ageS = (Date.now() - new Date(last).getTime()) / 1000;
    return ageS <= ONLINE_WINDOW_S ? 'online' : 'offline';
  }

  function hostName(id) {
    if (!id) return '\u2014';
    var h = state.hostsById[id];
    if (h) return h.name || h.host_name || String(id).slice(0, 8);
    return String(id).slice(0, 8);
  }

  function renderHosts(body) {
    var hosts = rowsOf(body, ['hosts', 'items', 'data']);
    state.hostsById = {};
    hosts.forEach(function (h) { if (h && h.id) state.hostsById[h.id] = h; });
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

  function svcActionButtons(s) {
    var id = esc(s.id);
    return '<span class="row-actions">' +
      '<button class="btn small" type="button" data-svc-action="restart" data-id="' + id + '">Restart</button>' +
      '<button class="btn small" type="button" data-svc-action="start" data-id="' + id + '">Start</button>' +
      '<button class="btn small danger" type="button" data-svc-action="stop" data-id="' + id + '">Stop</button>' +
      '</span>';
  }

  function renderServices(body) {
    var svcs = rowsOf(body, ['services', 'items', 'data']);
    if (!svcs.length) {
      el('servicesBody').innerHTML = emptyRow(8, 'No running services.');
      return;
    }
    el('servicesBody').innerHTML = svcs.map(function (s) {
      var ports = s.ports || {};
      var portList = Object.keys(ports).map(function (k) {
        return esc(k + ':' + ports[k]);
      }).join(', ') || '\u2014';
      var domains = (s.domains || []).map(esc).join(', ') || '\u2014';
      return '<tr>' +
        '<td><span class="mono">' + esc(s.project_name || s.name || s.service_name || short(s.id)) + '</span></td>' +
        '<td class="mono">' + esc(s.version || '\u2014') + '</td>' +
        '<td>' + badge(s.status || 'running') + '</td>' +
        '<td>' + badge(s.health_status || s.health || 'unknown') + '</td>' +
        '<td><span class="mono">' + esc(s.host_name || hostName(s.host_id) || '\u2014') + '</span></td>' +
        '<td class="mono">' + portList + '</td>' +
        '<td class="mono">' + domains + '</td>' +
        '<td class="actions-cell">' + svcActionButtons(s) + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderDeployments(body) {
    var deps = rowsOf(body, ['deployments', 'items', 'data']);
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
        '<td><span class="mono">' + esc(dep.host_name || hostName(dep.host_id) || '\u2014') + '</span></td>' +
        '<td>' + esc(timeAgo(dep.created_at)) + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderTasks(body) {
    var tasks = rowsOf(body, ['tasks', 'items', 'data']);
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

  function renderProjects(body) {
    var projects = rowsOf(body, ['projects', 'items', 'data']);
    if (!projects.length) {
      el('projectsBody').innerHTML = emptyRow(4, 'No projects.');
      return;
    }
    el('projectsBody').innerHTML = projects.map(function (p) {
      return '<tr>' +
        '<td><span class="mono">' + esc(p.name || short(p.id)) + '</span></td>' +
        '<td class="mono">' + esc(p.owner || '\u2014') + '</td>' +
        '<td class="mono">' + esc(p.runtime || '\u2014') + '</td>' +
        '<td>' + esc(timeAgo(p.updated_at || p.created_at)) + '</td>' +
        '</tr>';
    }).join('');
  }

  /* ---------------- domains panel ---------------- */

  // GET /v1/domains requires ?deployment_id= — the panel follows the
  // deployment picker, refreshed on the same 10s cadence as the rest.
  function domainsPath() {
    var sel = el('domainsDeploy');
    var id = sel && sel.value;
    return id ? '/domains?deployment_id=' + encodeURIComponent(id) : null;
  }

  function syncDeploySelects() {
    var deps = rowsOf(state.cache.deployments, ['deployments', 'items', 'data']);
    ['domainsDeploy', 'logsDeploy'].forEach(function (selId) {
      var sel = el(selId);
      if (!sel) return;
      var prev = sel.value;
      sel.innerHTML = deps.length
        ? deps.map(function (d) {
            var label = (d.project_name || d.project_id || 'project') + ' \u00b7 ' +
              (d.version || '?') + ' \u00b7 ' + String(d.id).slice(0, 8);
            return '<option value="' + esc(d.id) + '"' +
              (String(d.id) === String(prev) ? ' selected' : '') + '>' +
              esc(label) + '</option>';
          }).join('')
        : '<option value="">No deployments</option>';
    });
  }

  function renderDomains(body) {
    var domains = rowsOf(body, ['domains', 'items', 'data']);
    if (!domains.length) {
      el('domainsBody').innerHTML = emptyRow(7, 'No domains attached to this deployment.');
      return;
    }
    el('domainsBody').innerHTML = domains.map(function (d) {
      function flag(v) { return badge(v ? 'passing' : 'unknown'); }
      var https = d.https_reachable ? badge('passing')
        : (d.https_checked_at ? badge('failing') : badge('unknown'));
      return '<tr>' +
        '<td class="mono">' + esc(d.hostname || '\u2014') + '</td>' +
        '<td>' + badge(d.status) + '</td>' +
        '<td class="mono">' + esc(d.ingress || '\u2014') + '</td>' +
        '<td>' + flag(d.dns_configured) + '</td>' +
        '<td>' + flag(d.tunnel_configured) + '</td>' +
        '<td>' + https + '</td>' +
        '<td class="mono">' + esc(d.error || '\u2014') + '</td>' +
        '</tr>';
    }).join('');
  }

  /* ---------------- logs panel ---------------- */

  // Creates a real `logs` task for the selected deployment (POST /v1/tasks,
  // type=logs — read-only, needs read_status) and polls it to a terminal
  // state, mirroring the CLI `logs` command. User-triggered, not part of
  // the 10s auto-refresh: each fetch mints exactly one task.
  function logsTextOf(task) {
    var result = task && task.result;
    if (typeof result === 'string') return result;
    if (result && typeof result.logs === 'string') return result.logs;
    return '';
  }

  function logsUiError(msg) {
    var p = el('logsError');
    p.textContent = msg;
    p.hidden = false;
  }

  function logsReset() {
    if (state.logsTimer) { clearTimeout(state.logsTimer); state.logsTimer = null; }
    el('logsFetch').disabled = false;
    el('logsStop').hidden = true;
  }

  function fetchLogs() {
    var depId = el('logsDeploy').value;
    if (!depId) { logsUiError('Select a deployment first.'); return; }
    logsReset();
    el('logsError').hidden = true;
    el('logsOut').textContent = '';
    el('logsUpdated').textContent = 'starting logs task\u2026';
    el('logsFetch').disabled = true;
    el('logsStop').hidden = false;
    apiWrite('POST', '/tasks', { type: 'logs', payload: { deployment_id: depId } }).then(function (res) {
      var task = (res && res.task) || {};
      if (!task.id) throw new Error('control plane returned no task id');
      pollLogsTask(task.id);
    }).catch(function (err) {
      if (err && err.message === 'unauthorized') return; // requireAuth already ran
      logsUiError(err && err.code === 'forbidden'
        ? 'not permitted \u2014 this key lacks the read_status permission.'
        : 'failed to start logs task: ' + (err && err.message ? err.message : err));
      logsReset();
    });
  }

  function pollLogsTask(taskId) {
    var lastLen = 0;
    var terminal = { completed: 1, failed: 1, cancelled: 1 };
    function tick() {
      state.logsTimer = null;
      api('/tasks/' + taskId).then(function (res) {
        var task = (res && res.task) || {};
        var logs = logsTextOf(task);
        if (logs.length > lastLen) {
          var out = el('logsOut');
          out.textContent += logs.slice(lastLen);
          lastLen = logs.length;
          out.scrollTop = out.scrollHeight;
        }
        el('logsUpdated').textContent = 'task ' + String(taskId).slice(0, 8) + '\u2026 \u00b7 ' + (task.status || '?');
        if (terminal[task.status]) { logsReset(); return; }
        state.logsTimer = setTimeout(tick, 2000);
      }).catch(function (err) {
        if (err && err.message === 'unauthorized') { logsReset(); return; }
        logsUiError('log poll failed: ' + (err && err.message ? err.message : err));
        logsReset();
      });
    }
    tick();
  }

  /* ---------------- approvals ---------------- */

  // One approval dossier per task, fetched lazily and cached: everything an
  // approver needs, assembled from existing endpoints only —
  // task -> deployment -> project -> artifact (+ domain list).
  function fetchApprovalDetail(task) {
    var id = task.id;
    if (state.approvalDetail[id]) return state.approvalDetail[id];
    var payload = task.payload || {};

    function get(path, key) {
      return api(path).then(function (b) {
        return (b && b[key]) || null;
      }).catch(function () { return null; }); // partial dossier beats a failed panel
    }

    var jobs = {};
    if (payload.deployment_id) jobs.deployment = get('/deployments/' + payload.deployment_id, 'deployment');
    if (payload.project_id) jobs.project = get('/projects/' + payload.project_id, 'project');
    if (payload.artifact_id) jobs.artifact = get('/artifacts/' + payload.artifact_id, 'artifact');
    if (payload.deployment_id) jobs.domains = get('/domains?deployment_id=' + payload.deployment_id, 'domains');

    var p = Promise.all(Object.keys(jobs).map(function (k) {
      return jobs[k].then(function (v) { return [k, v]; });
    })).then(function (pairs) {
      var d = {};
      pairs.forEach(function (kv) { d[kv[0]] = kv[1]; });
      return d;
    });
    state.approvalDetail[id] = p;
    return p;
  }

  function actionSummary(task) {
    var payload = task.payload || {};
    switch (task.type) {
      case 'deploy':
        return 'Deploy version ' + (payload.version || '?') + ' to ' +
          hostName(payload.host_id || task.assigned_to);
      case 'rollback':
        return 'Roll back deployment ' + short(payload.deployment_id);
      case 'restart': case 'start': case 'stop':
        return task.type.charAt(0).toUpperCase() + task.type.slice(1) +
          ' service ' + short(payload.deployment_id);
      case 'remove':
        return 'Remove deployment ' + short(payload.deployment_id);
      default:
        return 'Run task type "' + (task.type || '?') + '"';
    }
  }

  function manifestOf(payload, project) {
    return payload.manifest || (project && project.configuration) || null;
  }

  function dossierHtml(task, detail) {
    var payload = task.payload || {};
    var project = detail.project || null;
    var artifact = detail.artifact || null;
    var deployment = detail.deployment || null;
    var domains = detail.domains || (deployment && deployment.domains) || [];
    var manifest = manifestOf(payload, project);
    var resources = manifest && manifest.resources;
    var env = manifest && manifest.env;

    function row(label, value) {
      return '<dt>' + esc(label) + '</dt><dd>' + value + '</dd>';
    }

    var html = '<div class="dossier"><dl>';
    html += row('Requested action', esc(actionSummary(task)));
    html += row('Project', esc((project && project.name) || payload.project_id || '\u2014'));
    html += row('Version', esc(payload.version || (deployment && deployment.version) || '\u2014'));
    html += row('Host', esc(hostName(payload.host_id || task.assigned_to || (deployment && deployment.host_id))));
    html += row('Artifact', artifact
      ? esc(artifact.filename || artifact.id) + ' &middot; ' + esc(fmtBytes(artifact.size))
      : esc(payload.artifact_id ? short(payload.artifact_id) + ' (unavailable)' : '\u2014'));
    if (payload.requested_host_port != null) {
      html += row('Requested host port', esc(payload.requested_host_port));
    }
    html += row('Resources', resources
      ? esc('memory=' + (resources.memory || '?') + ' cpu=' + (resources.cpu || '?'))
      : '<span class="muted-note">not specified</span>');
    html += row('Domains', domains && domains.length
      ? esc(domains.map(function (d) { return d.hostname || d; }).join(', '))
      : '<span class="muted-note">none</span>');
    html += row('Environment', env && Object.keys(env).length
      ? esc(Object.keys(env).join(', ') + ' (' + Object.keys(env).length + ' vars)')
      : '<span class="muted-note">none declared</span>');
    html += row('Task id', esc(task.id));
    html += '</dl>';
    html += '<div class="appr-actions">' +
      '<button class="btn small primary" type="button" data-approve="' + esc(task.id) + '">Approve</button>' +
      '<button class="btn small danger" type="button" data-reject="' + esc(task.id) + '">Reject</button>' +
      '<p class="appr-msg" id="apprMsg-' + esc(task.id) + '" hidden></p>' +
      '</div></div>';
    return html;
  }

  function renderApprovals(body) {
    var tasks = rowsOf(body, ['tasks', 'items', 'data']);
    if (!tasks.length) {
      el('approvalsBody').innerHTML = emptyRow(7, 'Nothing awaiting approval.');
      return;
    }
    el('approvalsBody').innerHTML = tasks.map(function (t) {
      var open = !!state.expanded[t.id];
      var summary =
        '<tr>' +
        '<td class="mono">' + short(t.id) + '</td>' +
        '<td class="mono">' + esc(t.type || '\u2014') + '</td>' +
        '<td class="mono">' + esc(short(t.payload && t.payload.project_id)) + '</td>' +
        '<td class="mono">' + esc((t.payload && t.payload.version) || '\u2014') + '</td>' +
        '<td><span class="mono">' + esc(hostName((t.payload && t.payload.host_id) || t.assigned_to)) + '</span></td>' +
        '<td>' + esc(timeAgo(t.created_at)) + '</td>' +
        '<td class="actions-cell"><button class="btn small" type="button" data-toggle="' + esc(t.id) + '">' +
        (open ? 'Hide' : 'Details') + '</button></td>' +
        '</tr>';
      var detail =
        '<tr class="appr-detail" data-detail-for="' + esc(t.id) + '"' + (open ? '' : ' hidden') + '>' +
        '<td colspan="7"><div class="dossier"><span class="muted-note">Loading dossier&hellip;</span></div></td>' +
        '</tr>';
      return summary + detail;
    }).join('');
    // Fill any dossiers that are currently expanded (cached after first load).
    tasks.forEach(function (t) {
      if (!state.expanded[t.id]) return;
      fetchApprovalDetail(t).then(function (detail) {
        var row = document.querySelector('tr.appr-detail[data-detail-for="' + t.id + '"]');
        if (row) row.querySelector('td').innerHTML = dossierHtml(t, detail);
      });
    });
  }

  function apprMsg(taskId, text, ok) {
    var m = el('apprMsg-' + taskId);
    if (!m) return;
    m.textContent = text;
    m.className = 'appr-msg ' + (ok ? 'ok' : 'err');
    m.hidden = false;
  }

  function afterDecision(taskId, verb, res) {
    var task = (res && res.task) || {};
    apprMsg(taskId, verb + 'd — task is now ' + (task.status || 'updated') + '.', true);
    delete state.approvalDetail[taskId]; // force a fresh dossier on next poll
    setTimeout(refreshAll, 800);
  }

  function decisionError(taskId, verb, err) {
    if (err && err.message === 'unauthorized') return; // requireAuth already ran
    var msg = err && err.code === 'forbidden'
      ? 'not permitted — this key lacks the approve_deployments permission'
      : 'failed: ' + (err && err.message ? err.message : err);
    apprMsg(taskId, msg, false);
  }

  function openApproveModal(taskId) {
    var tasks = rowsOf(state.cache.approvals, ['tasks', 'items', 'data']);
    var task = null;
    for (var i = 0; i < tasks.length; i++) {
      if (tasks[i].id === taskId) { task = tasks[i]; break; }
    }
    if (!task) return;
    state.pendingApprove = taskId;
    el('approveModalTitle').textContent = 'Approve this deployment?';
    el('approveModalResult').hidden = true;
    el('approveModalConfirm').disabled = false;
    fetchApprovalDetail(task).then(function (detail) {
      if (state.pendingApprove !== taskId) return; // user moved on
      var payload = task.payload || {};
      el('approveModalSummary').innerHTML = '<dl>' +
        '<dt>Action</dt><dd>' + esc(actionSummary(task)) + '</dd>' +
        '<dt>Project</dt><dd>' + esc((detail.project && detail.project.name) || payload.project_id || '\u2014') + '</dd>' +
        '<dt>Version</dt><dd>' + esc(payload.version || '\u2014') + '</dd>' +
        '<dt>Host</dt><dd>' + esc(hostName(payload.host_id || task.assigned_to)) + '</dd>' +
        '<dt>Artifact</dt><dd>' + (detail.artifact
          ? esc(detail.artifact.filename || detail.artifact.id) + ' &middot; ' + esc(fmtBytes(detail.artifact.size))
          : '\u2014') + '</dd>' +
        '</dl>' +
        '<p class="muted-note" style="color:var(--muted);font-size:12px">' +
        'Approving queues the task for the host worker to execute. This cannot be undone from here — ' +
        'the task can still be cancelled afterwards while it is not running.</p>';
    });
    el('approveModal').hidden = false;
  }

  function closeApproveModal() {
    el('approveModal').hidden = true;
    state.pendingApprove = null;
  }

  /* ---------------- service actions ---------------- */

  function svcMsg(text, isErr) {
    var p = el('servicesError');
    p.textContent = text;
    p.hidden = false;
    if (!isErr) {
      setTimeout(function () { p.hidden = true; }, 4000);
    }
  }

  function serviceAction(id, action) {
    var label = action === 'stop' ? 'Stop' : action === 'start' ? 'Start' : 'Restart';
    if (action === 'stop') {
      // Destructive: stop takes the service's containers down.
      if (!window.confirm('Stop service ' + id.slice(0, 8) + '\u2026? Its containers will be taken down.')) return;
    }
    apiWrite('POST', '/services/' + id + '/' + action).then(function (res) {
      var task = (res && res.task) || {};
      svcMsg(label + ' requested — control task ' + (task.id ? task.id.slice(0, 8) + '\u2026 ' : '') + 'queued.', false);
      setTimeout(refreshAll, 800);
    }).catch(function (err) {
      if (err && err.message === 'unauthorized') return; // requireAuth already ran
      if (err && err.code === 'forbidden') {
        svcMsg('not permitted — this key lacks the ' + (action === 'stop' ? 'stop' : 'restart') + ' permission.', true);
      } else {
        svcMsg(label + ' failed: ' + (err && err.message ? err.message : err), true);
      }
    });
  }

  /* ---------------- overview strip ---------------- */

  function setStat(id, text, tone) {
    var s = el(id);
    s.textContent = text;
    s.classList.remove('bad', 'warn');
    if (tone) s.classList.add(tone);
  }

  var INACTIVE_DEPLOYMENT = { failed: 1, stopped: 1, rolled_back: 1 };

  function renderOverview() {
    var c = state.cache;

    var hosts = rowsOf(c.hosts, ['hosts', 'items', 'data']);
    var online = hosts.filter(function (h) { return hostStatus(h) === 'online'; }).length;
    setStat('statHosts', hosts.length ? online + '/' + hosts.length : '\u2014');

    var events = rowsOf(c.recentEvents, ['events', 'items', 'data']);
    var cutoff = Date.now() - AGENT_ACTIVE_WINDOW_MS;
    var agents = {};
    events.forEach(function (e) {
      if (e && e.actor_type === 'agent' && e.actor_id) {
        var t = new Date(e.created_at).getTime();
        if (!isNaN(t) && t >= cutoff) agents[e.actor_id] = 1;
      }
    });
    setStat('statAgents', String(Object.keys(agents).length));

    var svcs = rowsOf(c.services, ['services', 'items', 'data']);
    var running = svcs.filter(function (s) { return String(s.status).toLowerCase() === 'running'; }).length;
    setStat('statApps', String(running));

    var deps = rowsOf(c.deployments, ['deployments', 'items', 'data']);
    var active = deps.filter(function (d) { return !INACTIVE_DEPLOYMENT[String(d.status).toLowerCase()]; }).length;
    setStat('statDeployments', String(active));
    var dayAgo = Date.now() - 24 * 3600 * 1000;
    var failed24 = deps.filter(function (d) {
      if (String(d.status).toLowerCase() !== 'failed') return false;
      var t = new Date(d.created_at).getTime();
      return !isNaN(t) && t >= dayAgo;
    }).length;
    setStat('statFailed', String(failed24), failed24 > 0 ? 'bad' : null);

    var pending = rowsOf(c.approvals, ['tasks', 'items', 'data']).length;
    setStat('statApprovals', String(pending), pending > 0 ? 'warn' : null);
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
    { id: 'approvals',   path: '/tasks?status=awaiting_approval&limit=50', render: renderApprovals },
    { id: 'hosts',       path: '/hosts',            render: renderHosts },
    { id: 'services',    path: '/services',         render: renderServices },
    { id: 'deployments', path: '/deployments?limit=200', render: renderDeployments },
    { id: 'tasks',       path: '/tasks?limit=20',   render: renderTasks },
    { id: 'projects',    path: '/projects',         render: renderProjects },
    // pathFor: computed per refresh — domains needs the picker's deployment.
    { id: 'domains',     pathFor: domainsPath,      render: renderDomains }
    // Logs is deliberately NOT auto-polled: fetching mints a real logs
    // task, so it is user-triggered via the Fetch button above.
  ];

  function refreshAll() {
    if (!state.key) return;
    var anyOk = false;
    var jobs = PANELS.map(function (p) {
      var path = typeof p.pathFor === 'function' ? p.pathFor() : p.path;
      if (!path) return Promise.resolve(); // e.g. domains with no deployment picked yet
      return api(path).then(function (body) {
        state.cache[p.id] = body;
        p.render(body);
        if (p.id === 'deployments') syncDeploySelects();
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
    // Recent events feed the "agents active" overview stat (there is no
    // GET /v1/agents list endpoint, so counts come from actor activity).
    jobs.push(
      api('/events?since=' + encodeURIComponent(new Date(Date.now() - AGENT_ACTIVE_WINDOW_MS).toISOString()) + '&limit=200')
        .then(function (body) { state.cache.recentEvents = body; })
        .catch(function () { state.cache.recentEvents = null; })
    );
    Promise.all(jobs).then(function () {
      renderOverview();
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

  // Approvals table: delegated clicks for Details / Approve / Reject.
  el('approvalsBody').addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof HTMLElement)) return;
    var toggleId = t.getAttribute('data-toggle');
    if (toggleId) {
      var taskId = toggleId;
      state.expanded[taskId] = !state.expanded[taskId];
      if (state.expanded[taskId]) {
        // Fetch the dossier now (cached), then re-render to fill it.
        var tasks = rowsOf(state.cache.approvals, ['tasks', 'items', 'data']);
        var task = null;
        for (var i = 0; i < tasks.length; i++) {
          if (tasks[i].id === taskId) { task = tasks[i]; break; }
        }
        if (task) {
          fetchApprovalDetail(task).then(function () {
            if (state.cache.approvals) renderApprovals(state.cache.approvals);
          });
        }
      }
      if (state.cache.approvals) renderApprovals(state.cache.approvals);
      return;
    }
    var approveId = t.getAttribute('data-approve');
    if (approveId) { openApproveModal(approveId); return; }
    var rejectId = t.getAttribute('data-reject');
    if (rejectId) {
      if (!window.confirm('Reject task ' + rejectId.slice(0, 8) + '\u2026? It will be cancelled and never executed.')) return;
      apiWrite('POST', '/tasks/' + rejectId + '/reject')
        .then(function (res) { afterDecision(rejectId, 'Reject', res); })
        .catch(function (err) { decisionError(rejectId, 'Reject', err); });
    }
  });

  // Applications table: delegated clicks for Restart / Stop / Start.
  el('servicesBody').addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof HTMLElement)) return;
    var action = t.getAttribute('data-svc-action');
    var id = t.getAttribute('data-id');
    if (action && id) serviceAction(id, action);
  });

  // Approve confirmation modal.
  el('approveModalCancel').addEventListener('click', closeApproveModal);

  // Logs panel: user-triggered fetch (mints one real logs task per click).
  el('logsFetch').addEventListener('click', fetchLogs);
  el('logsStop').addEventListener('click', logsReset);
  // Switching the domains deployment re-fetches on the next 10s tick;
  // fetch immediately so the panel doesn't sit stale.
  el('domainsDeploy').addEventListener('change', function () {
    var path = domainsPath();
    if (!path || !state.key) return;
    api(path).then(function (body) {
      state.cache.domains = body;
      renderDomains(body);
      panelOk('domains');
    }).catch(function (err) {
      if (err && err.message === 'unauthorized') return;
      panelError('domains', err);
    });
  });  el('approveModal').addEventListener('click', function (e) {
    if (e.target === el('approveModal')) closeApproveModal(); // backdrop click
  });
  el('approveModalConfirm').addEventListener('click', function () {
    var taskId = state.pendingApprove;
    if (!taskId) return;
    el('approveModalConfirm').disabled = true;
    var result = el('approveModalResult');
    result.hidden = true;
    apiWrite('POST', '/tasks/' + taskId + '/approve').then(function (res) {
      var task = (res && res.task) || {};
      result.textContent = 'Approved — task is now ' + (task.status || 'queued') + '.';
      result.className = 'modal-result ok';
      result.hidden = false;
      el('approveModalConfirm').disabled = true;
      delete state.approvalDetail[taskId];
      delete state.expanded[taskId];
      setTimeout(function () { closeApproveModal(); refreshAll(); }, 900);
    }).catch(function (err) {
      if (err && err.message === 'unauthorized') return; // requireAuth already ran
      result.textContent = err && err.code === 'forbidden'
        ? 'not permitted — this key lacks the approve_deployments permission.'
        : 'Approve failed: ' + (err && err.message ? err.message : err);
      result.className = 'modal-result err';
      result.hidden = false;
      el('approveModalConfirm').disabled = false;
    });
  });

  // Boot: key already in this tab's session storage? Go straight in.
  if (state.key) {
    hideAuth();
    startLoops();
  } else {
    showAuth();
  }
})();
