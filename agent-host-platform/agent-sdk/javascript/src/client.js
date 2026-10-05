/**
 * uaht-sdk — JavaScript client for the Universal Agent-to-Persistent-Host
 * Deployment System control plane (wire protocol v1).
 *
 * Node 18+, zero dependencies. Implements protocol §1 (auth), §2
 * (idempotency + error shape), §3 (endpoints), and §3.8 (SSE stream).
 */

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { stat } from "node:fs/promises";
import { Readable } from "node:stream";

export class UahtError extends Error {
  /** @param {string} code - protocol error code (e.g. "forbidden") */
  constructor(code, message, status = null, body = null) {
    super(message);
    this.name = "UahtError";
    this.code = code;
    this.status = status;
    this.body = body;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class UahtClient {
  /**
   * @param {{baseUrl: string, apiKey: string, fetch?: typeof fetch, userAgent?: string}} opts
   */
  constructor({ baseUrl, apiKey, fetch: fetchImpl, userAgent = "uaht-sdk/1.0.0" }) {
    if (!baseUrl) throw new TypeError("baseUrl is required");
    if (!apiKey) throw new TypeError("apiKey is required");
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.fetch = fetchImpl || globalThis.fetch.bind(globalThis);
    this.userAgent = userAgent;
  }

  _headers(extra = {}) {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      "User-Agent": this.userAgent,
      ...extra,
    };
  }

  async _request(method, path, { body, query, headers } = {}) {
    let url = `${this.baseUrl}/v1${path}`;
    if (query) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) qs.set(k, String(v));
      }
      const s = qs.toString();
      if (s) url += `?${s}`;
    }
    const opts = {
      method,
      headers: this._headers({
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      }),
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    };
    let res;
    try {
      res = await this.fetch(url, opts);
    } catch (err) {
      throw new UahtError("connection_error", `request failed: ${err.message}`);
    }
    if (res.status === 204) return null;
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        throw new UahtError("bad_response", "server returned non-JSON body", res.status, text);
      }
    }
    if (!res.ok) {
      const err = data && data.error ? data.error : {};
      throw new UahtError(
        err.code || "unknown_error",
        err.message || `request failed with status ${res.status}`,
        res.status,
        data
      );
    }
    return data;
  }

  _get(path, query) {
    return this._request("GET", path, { query });
  }
  _post(path, body, query) {
    return this._request("POST", path, { body, query });
  }
  _put(path, body) {
    return this._request("PUT", path, { body });
  }
  _delete(path, body) {
    return this._request("DELETE", path, { body });
  }

  // ---- §3.1 Agents ----
  registerAgent({ name, type, capabilities, permissions }) {
    return this._post("/agents/register", { name, type, capabilities, permissions });
  }
  me() {
    return this._get("/agents/me");
  }

  // ---- §3.2 Tasks ----
  createTask({ type, payload = {}, idempotency_key, priority, host_id, mode }) {
    return this._post("/tasks", { type, payload, idempotency_key, priority, host_id, mode });
  }
  getTask(id) {
    return this._get(`/tasks/${id}`);
  }
  listTasks({ status, type, host_id, limit, cursor } = {}) {
    return this._get("/tasks", { status, type, host_id, limit, cursor });
  }
  cancelTask(id) {
    return this._post(`/tasks/${id}/cancel`, {});
  }
  approveTask(id) {
    return this._post(`/tasks/${id}/approve`, {});
  }
  rejectTask(id) {
    return this._post(`/tasks/${id}/reject`, {});
  }

  /** Extract the log text from a logs task row (§5: result = {logs: "..."}). */
  static _logsText(task) {
    const result = task && task.result;
    if (typeof result === "string") return result;
    if (result && typeof result.logs === "string") return result.logs;
    return "";
  }

  /**
   * Create a `logs` task for a deployment (or poll an existing logs task)
   * until it reaches a terminal state, then return the log text.
   * Mirrors the CLI `logs` command.
   *
   * With `follow: true` returns an async generator that yields each new log
   * chunk as it arrives (like `logs --follow`) until the task terminates.
   *
   * @param {{deploymentId?: string, taskId?: string, follow?: boolean,
   *          pollIntervalMs?: number, timeoutMs?: number}} opts
   * @returns {Promise<string> | AsyncGenerator<string>}
   */
  async getLogs({ deploymentId, taskId, follow = false, pollIntervalMs = 3000, timeoutMs = 600000 } = {}) {
    const task = await this._resolveLogsTask({ deploymentId, taskId });
    if (follow) return this._tailLogs(task, { pollIntervalMs });
    const terminal = new Set(["completed", "failed", "cancelled"]);
    const started = Date.now();
    let current = task;
    for (;;) {
      const cur = await this.getTask(current.id);
      current = cur.task || cur;
      if (terminal.has(current.status)) break;
      if (Date.now() - started > timeoutMs) {
        throw new UahtError("timeout", `logs task ${current.id} did not finish within ${timeoutMs}ms`);
      }
      await sleep(pollIntervalMs);
    }
    return UahtClient._logsText(current);
  }

  /** Resolve deploymentId -> new logs task, or taskId -> the task itself. */
  async _resolveLogsTask({ deploymentId, taskId }) {
    if (!deploymentId && !taskId) {
      throw new TypeError("deploymentId or taskId is required");
    }
    if (taskId) {
      const cur = await this.getTask(taskId);
      return cur.task || cur;
    }
    const created = await this.createTask({ type: "logs", payload: { deployment_id: deploymentId } });
    return created.task || created;
  }

  /** Async generator of incremental log chunks until the task terminates.
   * The already-fetched task is processed first so its current logs are
   * yielded before the first re-poll. */
  async *_tailLogs(task, { pollIntervalMs = 2000 } = {}) {
    const terminal = new Set(["completed", "failed", "cancelled"]);
    let lastLen = 0;
    for (;;) {
      const logs = UahtClient._logsText(task);
      const chunk = logs.slice(lastLen);
      if (chunk) {
        yield chunk;
        lastLen = logs.length;
      }
      if (terminal.has(task.status)) break;
      await sleep(pollIntervalMs);
      const cur = await this.getTask(task.id);
      task = cur.task || cur;
    }
  }

  // ---- §3.4 Hosts ----
  listHosts() {
    return this._get("/hosts");
  }
  getHost(id) {
    return this._get(`/hosts/${id}`);
  }

  // ---- §3.5 Projects & artifacts ----
  createProject({ name, owner, repository, runtime, configuration }) {
    return this._post("/projects", { name, owner, repository, runtime, configuration });
  }
  listProjects({ limit, cursor } = {}) {
    return this._get("/projects", { limit, cursor });
  }
  getProject(id) {
    return this._get(`/projects/${id}`);
  }
  updateProject(id, configuration) {
    return this._put(`/projects/${id}`, { configuration });
  }
  listArtifacts({ project_id, limit, cursor } = {}) {
    return this._get("/artifacts", { project_id, limit, cursor });
  }
  getArtifact(id) {
    return this._get(`/artifacts/${id}`);
  }

  /**
   * Step 1 of artifact upload (§3.5): compute sha256 of the file client-side,
   * register the artifact, and return the server-allocated upload URL.
   * @returns {{artifact: object, upload_url: string}}
   */
  async initArtifact({ project_id, filePath, version, filename }) {
    const st = await stat(filePath);
    // Split on both separators so Windows-style paths resolve correctly.
    const name = filename || filePath.split(/[\\/]/).pop();
    const hash = createHash("sha256");
    for await (const chunk of createReadStream(filePath)) hash.update(chunk);
    const checksum = `sha256:${hash.digest("hex")}`;
    return this._post("/artifacts/init", {
      project_id,
      filename: name,
      size: st.size,
      checksum,
      version,
    });
  }

  /**
   * Step 2 of artifact upload (§3.5): stream raw bytes to the upload URL.
   * Content-Type is application/octet-stream per the protocol.
   */
  async uploadArtifact(uploadUrl, filePath) {
    const stream = createReadStream(filePath);
    const res = await this.fetch(`${this.baseUrl}${uploadUrl}`, {
      method: "PUT",
      headers: this._headers({ "Content-Type": "application/octet-stream" }),
      body: stream,
      duplex: "half",
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {}
      const err = data && data.error ? data.error : {};
      throw new UahtError(
        err.code || "unknown_error",
        err.message || `artifact upload failed with status ${res.status}`,
        res.status,
        data
      );
    }
    return true;
  }

  /** Stream an artifact's bytes to a local file. */
  async downloadArtifact(id, filePath) {
    const res = await this.fetch(`${this.baseUrl}/v1/artifacts/${id}/download`, {
      method: "GET",
      headers: this._headers(),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {}
      const err = data && data.error ? data.error : {};
      throw new UahtError(
        err.code || "unknown_error",
        err.message || `artifact download failed with status ${res.status}`,
        res.status,
        data
      );
    }
    const out = createWriteStream(filePath);
    await new Promise((resolve, reject) => {
      Readable.fromWeb(res.body).pipe(out).on("finish", resolve).on("error", reject);
    });
    return filePath;
  }

  // ---- §3.6 Deployments ----
  createDeployment({ project_id, host_id, version, artifact_id, mode, idempotency_key }) {
    return this._post("/deployments", {
      project_id,
      host_id,
      version,
      artifact_id,
      mode,
      idempotency_key,
    });
  }
  getDeployment(id) {
    return this._get(`/deployments/${id}`);
  }
  listDeployments({ project_id, host_id, status, limit, cursor } = {}) {
    return this._get("/deployments", { project_id, host_id, status, limit, cursor });
  }
  rollbackDeployment(id) {
    return this._post(`/deployments/${id}/rollback`, {});
  }

  // ---- §3.10 Domains ----
  addDomain(deploymentId, hostname, ingress) {
    const body = { deployment_id: deploymentId, hostname };
    if (ingress !== undefined && ingress !== null) body.ingress = ingress; // 'tunnel' | 'direct' (Phase 7)
    return this._post("/domains", body);
  }
  listDomains(deploymentId) {
    return this._get("/domains", { deployment_id: deploymentId });
  }
  removeDomain(deploymentId, hostname) {
    return this._delete("/domains", { deployment_id: deploymentId, hostname });
  }

  // ---- §3.6 Services ----
  listServices({ host_id } = {}) {
    return this._get("/services", { host_id });
  }
  restartService(id) {
    return this._post(`/services/${id}/restart`, {});
  }
  stopService(id) {
    return this._post(`/services/${id}/stop`, {});
  }
  startService(id) {
    return this._post(`/services/${id}/start`, {});
  }

  // ---- §3.7 Secrets ----
  setSecret(projectId, name, value) {
    return this._post(`/projects/${projectId}/secrets`, { name, value });
  }
  listSecrets(projectId) {
    return this._get(`/projects/${projectId}/secrets`);
  }
  deleteSecret(projectId, name) {
    return this._delete(`/projects/${projectId}/secrets/${encodeURIComponent(name)}`);
  }

  // ---- §3.8 Events ----
  listEvents({ type, since, limit, cursor } = {}) {
    return this._get("/events", { type, since, limit, cursor });
  }

  /**
   * Async generator over the SSE event stream (§3.8).
   * Implements SSE parsing on the fetch reader — no extra dependency.
   * Yields parsed event objects; stops when the consumer breaks or the
   * optional `signal` aborts.
   *
   * `since` (ISO-8601) and `limit` are appended as query parameters;
   * the server replays the last `limit` events before live-pushing.
   */
  async *streamEvents({ since, limit, signal } = {}) {
    const qs = new URLSearchParams();
    if (since) qs.set("since", String(since));
    if (limit !== undefined && limit !== null) qs.set("limit", String(limit));
    const s = qs.toString();
    const res = await this.fetch(`${this.baseUrl}/v1/events/stream${s ? `?${s}` : ""}`, {
      method: "GET",
      headers: this._headers({ Accept: "text/event-stream" }),
      signal,
    });
    if (!res.ok || !res.body) {
      const text = res.body ? await res.text().catch(() => "") : "";
      throw new UahtError(
        "stream_error",
        `event stream failed with status ${res.status}`,
        res.status,
        text || null
      );
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLines = [];
          for (const line of raw.split("\n")) {
            const l = line.trimEnd();
            if (l.startsWith("data:")) dataLines.push(l.slice(5).trimStart());
            else if (l.startsWith(":")) continue; // comment / heartbeat
            // event: / id: / retry: fields are ignored (protocol v1 is data-only)
          }
          if (dataLines.length) {
            const payload = dataLines.join("\n");
            try {
              yield JSON.parse(payload);
            } catch {
              // ignore malformed frames; the stream stays open
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  // ---- §3.9 Misc ----
  health() {
    return this._get("/health");
  }

  /**
   * Convenience helper: resolve a project name to its id, create a
   * deployment for `version`, and optionally wait until the backing task
   * reaches a terminal state.
   *
   * @param {{project: string, host?: string, version: string, artifactId?: string,
   *          mode?: "automatic"|"manual", wait?: boolean, pollIntervalMs?: number,
   *          timeoutMs?: number}} opts
   * @returns {{deployment: object, task: object}}
   */
  async deploy({ project, host, version, artifactId, mode, wait = false,
                 pollIntervalMs = 3000, timeoutMs = 600000 } = {}) {
    if (!project) throw new TypeError("project (name) is required");
    if (!version) throw new TypeError("version is required");

    const projects = await this.listProjects();
    const rows = projects.projects || projects || [];
    const match = rows.find((p) => p.name === project || p.id === project);
    if (!match) throw new UahtError("not_found", `project "${project}" not found`);

    let hostId;
    if (host) {
      const hosts = await this.listHosts();
      const hrows = hosts.hosts || hosts || [];
      const hm = hrows.find((h) => h.name === host || h.id === host);
      if (!hm) throw new UahtError("not_found", `host "${host}" not found`);
      hostId = hm.id;
    }

    const created = await this.createDeployment({
      project_id: match.id,
      host_id: hostId,
      version,
      artifact_id: artifactId,
      mode,
    });
    let deployment = created.deployment || created;
    let task = created.task || null;

    if (wait && task && task.id) {
      const terminal = new Set(["completed", "failed", "cancelled"]);
      const started = Date.now();
      for (;;) {
        const cur = await this.getTask(task.id);
        task = cur.task || cur;
        if (terminal.has(task.status)) break;
        if (Date.now() - started > timeoutMs) {
          throw new UahtError("timeout", `deploy wait timed out after ${timeoutMs}ms`);
        }
        await sleep(pollIntervalMs);
      }
      const dep = await this.getDeployment(deployment.id);
      deployment = dep.deployment || dep;
    }
    return { deployment, task };
  }
}
