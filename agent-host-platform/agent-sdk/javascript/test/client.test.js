/**
 * Contract tests for uaht-sdk: mock `fetch`, assert URLs, methods, headers,
 * idempotency_key passthrough, and protocol error-shape parsing.
 */
import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { UahtClient, UahtError } from "../src/index.js";

const calls = [];

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
    body: null,
  };
}

function mockFetch(response) {
  return async (url, opts) => {
    calls.push({ url, opts });
    return response;
  };
}

beforeEach(() => {
  calls.length = 0;
});

describe("request plumbing", () => {
  it("sends base URL + bearer token + user agent", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com/",
      apiKey: "sekret",
      fetch: mockFetch(jsonResponse(200, { agent: { name: "a" } })),
    });
    await client.me();
    assert.equal(calls.length, 1);
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/agents/me");
    assert.equal(opts.method, "GET");
    assert.equal(opts.headers.Authorization, "Bearer sekret");
    assert.ok(opts.headers["User-Agent"].startsWith("uaht-sdk/"));
  });

  it("serializes query params and drops undefined/null", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { tasks: [] })),
    });
    await client.listTasks({ status: "running", type: undefined, limit: 10, cursor: null });
    const { url } = calls[0];
    assert.ok(url.startsWith("https://cp.example.com/v1/tasks?"));
    assert.ok(url.includes("status=running"));
    assert.ok(url.includes("limit=10"));
    assert.ok(!url.includes("type="));
    assert.ok(!url.includes("cursor="));
  });

  it("parses the protocol error shape into UahtError", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(
        jsonResponse(403, { error: { code: "forbidden", message: "missing permission: deploy" } })
      ),
    });
    await assert.rejects(() => client.getDeployment("d1"), (err) => {
      assert.ok(err instanceof UahtError);
      assert.equal(err.code, "forbidden");
      assert.equal(err.message, "missing permission: deploy");
      assert.equal(err.status, 403);
      return true;
    });
  });

  it("wraps connection failures as UahtError connection_error", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async () => {
        throw new Error("socket hang up");
      },
    });
    await assert.rejects(() => client.health(), (err) => {
      assert.ok(err instanceof UahtError);
      assert.equal(err.code, "connection_error");
      return true;
    });
  });
});

describe("tasks", () => {
  it("createTask posts JSON with idempotency_key passthrough", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { task: { id: "t1" } })),
    });
    await client.createTask({
      type: "restart",
      payload: { deployment_id: "d1" },
      idempotency_key: "idem-123",
      priority: 5,
    });
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/tasks");
    assert.equal(opts.method, "POST");
    assert.equal(opts.headers["Content-Type"], "application/json");
    const body = JSON.parse(opts.body);
    assert.equal(body.type, "restart");
    assert.deepEqual(body.payload, { deployment_id: "d1" });
    assert.equal(body.idempotency_key, "idem-123");
    assert.equal(body.priority, 5);
  });

  it("cancel/approve/reject hit the right endpoints", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { task: {} })),
    });
    await client.cancelTask("t1");
    await client.approveTask("t2");
    await client.rejectTask("t3");
    assert.equal(calls[0].url, "https://cp.example.com/v1/tasks/t1/cancel");
    assert.equal(calls[1].url, "https://cp.example.com/v1/tasks/t2/approve");
    assert.equal(calls[2].url, "https://cp.example.com/v1/tasks/t3/reject");
    for (const c of calls) assert.equal(c.opts.method, "POST");
  });
});

describe("deployments and services", () => {
  it("createDeployment passes idempotency_key and mode", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { deployment: { id: "d1" }, task: { id: "t1" } })),
    });
    await client.createDeployment({
      project_id: "p1",
      version: "1.0.0",
      mode: "manual",
      idempotency_key: "idem-9",
    });
    const body = JSON.parse(calls[0].opts.body);
    assert.equal(body.idempotency_key, "idem-9");
    assert.equal(body.mode, "manual");
    assert.equal(calls[0].opts.method, "POST");
  });

  it("restart/stop/start service endpoints", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { task: { id: "t1" } })),
    });
    await client.restartService("s1");
    await client.stopService("s2");
    await client.startService("s3");
    assert.equal(calls[0].url, "https://cp.example.com/v1/services/s1/restart");
    assert.equal(calls[1].url, "https://cp.example.com/v1/services/s2/stop");
    assert.equal(calls[2].url, "https://cp.example.com/v1/services/s3/start");
  });

  // W15 contract conformance (2026-10-05): the wire protocol is
  // authoritative — SDK signatures expose exactly the params the server
  // implements (no dead params the server ignores), and every stable
  // capability exists in both SDKs.
  it("createDeployment passes host_port for fixed-port reservation", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { deployment: { id: "d1" }, task: { id: "t1" } })),
    });
    await client.createDeployment({
      project_id: "p1",
      host_id: "h1",
      version: "1.0.0",
      host_port: 8080,
    });
    const body = JSON.parse(calls[0].opts.body);
    assert.equal(body.host_port, 8080);
    assert.equal(body.host_id, "h1");
  });

  it("updateProject sends configuration, repository, and runtime (PUT wire shape)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { project: { id: "p1" } })),
    });
    await client.updateProject(
      "p1",
      { env: { A: "1" } },
      { repository: "https://example.com/r.git", runtime: "docker" }
    );
    const body = JSON.parse(calls[0].opts.body);
    assert.deepEqual(body, {
      configuration: { env: { A: "1" } },
      repository: "https://example.com/r.git",
      runtime: "docker",
    });
    assert.equal(calls[0].opts.method, "PUT");
  });

  it("updateProject stays backwards compatible with (id, configuration)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { project: { id: "p1" } })),
    });
    await client.updateProject("p1", { env: {} });
    const body = JSON.parse(calls[0].opts.body);
    assert.deepEqual(body, { configuration: { env: {} } });
  });

  it("listProjects sends no dead params (wire has no pagination here)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { projects: [] })),
    });
    await client.listProjects();
    assert.equal(calls[0].url, "https://cp.example.com/v1/projects");
  });

  it("listArtifacts sends only project_id (wire has no limit/cursor here)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { artifacts: [] })),
    });
    await client.listArtifacts({ project_id: "p1" });
    assert.equal(calls[0].url, "https://cp.example.com/v1/artifacts?project_id=p1");
  });

  it("listDeployments sends no cursor (wire supports project_id/host_id/status/limit)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { deployments: [] })),
    });
    await client.listDeployments({ project_id: "p1", status: "running", limit: 10 });
    const { url } = calls[0];
    assert.ok(url.includes("project_id=p1"));
    assert.ok(url.includes("status=running"));
    assert.ok(url.includes("limit=10"));
    assert.ok(!url.includes("cursor="));
  });

  it("listServices supports host_id and limit", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { services: [] })),
    });
    await client.listServices({ host_id: "h1", limit: 5 });
    const { url } = calls[0];
    assert.ok(url.includes("host_id=h1"));
    assert.ok(url.includes("limit=5"));
  });

  it("deploy() passes hostPort through to createDeployment", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        if (url.endsWith("/v1/projects")) return jsonResponse(200, { projects: [{ id: "p-1", name: "web" }] });
        return jsonResponse(201, { deployment: { id: "d-1" }, task: { id: "t-1" } });
      },
    });
    await client.deploy({ project: "web", version: "1.0.0", hostPort: 9090 });
    const deployCall = calls.find((c) => c.url.endsWith("/v1/deployments"));
    const body = JSON.parse(deployCall.opts.body);
    assert.equal(body.host_port, 9090);
  });
});

describe("secrets", () => {
  it("set/list/delete use the project-scoped paths", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, {})),
    });
    await client.setSecret("p1", "DB_PASS", "x");
    await client.listSecrets("p1");
    await client.deleteSecret("p1", "DB/PASS");
    assert.equal(calls[0].url, "https://cp.example.com/v1/projects/p1/secrets");
    assert.equal(calls[0].opts.method, "POST");
    assert.deepEqual(JSON.parse(calls[0].opts.body), { name: "DB_PASS", value: "x" });
    assert.equal(calls[1].opts.method, "GET");
    assert.equal(calls[2].url, "https://cp.example.com/v1/projects/p1/secrets/DB%2FPASS");
    assert.equal(calls[2].opts.method, "DELETE");
  });
});

describe("deploy() helper", () => {
  it("resolves project name -> id and creates a deployment", async () => {
    const seen = [];
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        seen.push({ url, opts });
        if (url.endsWith("/v1/projects"))
          return jsonResponse(200, { projects: [{ id: "p-1", name: "my-api" }] });
        if (url.endsWith("/v1/deployments"))
          return jsonResponse(201, {
            deployment: { id: "d-1", status: "requested" },
            task: { id: "t-1", status: "queued" },
          });
        throw new Error(`unexpected ${url}`);
      },
    });
    const { deployment, task } = await client.deploy({ project: "my-api", version: "2.0.0" });
    assert.equal(deployment.id, "d-1");
    assert.equal(task.id, "t-1");
    const depCall = seen.find((c) => c.url.endsWith("/v1/deployments"));
    assert.deepEqual(JSON.parse(depCall.opts.body), {
      project_id: "p-1",
      version: "2.0.0",
    });
  });

  it("resolves host name and waits for terminal task status", async () => {
    const states = [{ task: { id: "t-1", status: "running" } }, { task: { id: "t-1", status: "completed" } }];
    let taskCalls = 0;
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url) => {
        if (url.endsWith("/v1/projects"))
          return jsonResponse(200, { projects: [{ id: "p-1", name: "my-api" }] });
        if (url.endsWith("/v1/hosts"))
          return jsonResponse(200, { hosts: [{ id: "h-1", name: "host-01" }] });
        if (url.endsWith("/v1/deployments"))
          return jsonResponse(201, {
            deployment: { id: "d-1" },
            task: { id: "t-1", status: "queued" },
          });
        if (url.includes("/v1/tasks/t-1")) return jsonResponse(200, states[taskCalls++]);
        if (url.includes("/v1/deployments/d-1"))
          return jsonResponse(200, { deployment: { id: "d-1", status: "running" } });
        throw new Error(`unexpected ${url}`);
      },
    });
    const { deployment, task } = await client.deploy({
      project: "my-api",
      host: "host-01",
      version: "1.0.0",
      wait: true,
      pollIntervalMs: 1,
    });
    assert.equal(task.status, "completed");
    assert.equal(deployment.status, "running");
    assert.ok(taskCalls >= 2);
  });
});

describe("streamEvents", () => {
  it("parses SSE frames into event objects", async () => {
    const frames = [
      'data: {"id":1,"type":"task.created"}\n\n',
      ': heartbeat\n\ndata: {"id":2,"type":"deployment.completed"}\n',
      '\n',
    ].join("");
    const bytes = new TextEncoder().encode(frames);
    const stream = new ReadableStream({
      start(c) {
        c.enqueue(bytes);
        c.close();
      },
    });
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        assert.equal(url, "https://cp.example.com/v1/events/stream");
        assert.equal(opts.headers.Accept, "text/event-stream");
        return { ok: true, status: 200, body: stream };
      },
    });
    const got = [];
    for await (const ev of client.streamEvents()) got.push(ev);
    assert.deepEqual(got, [
      { id: 1, type: "task.created" },
      { id: 2, type: "deployment.completed" },
    ]);
  });
});

describe("artifact upload headers", () => {
  it("uploadArtifact uses application/octet-stream on the returned upload_url", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        return { ok: true, status: 200, text: async () => "" };
      },
    });
    await client.uploadArtifact("/v1/artifacts/a1/content", fileURLToPath(import.meta.url));
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/artifacts/a1/content");
    assert.equal(opts.method, "PUT");
    assert.equal(opts.headers["Content-Type"], "application/octet-stream");
    assert.equal(opts.headers.Authorization, "Bearer k");
  });
});

describe("domains (§3.10)", () => {
  it("addDomain POSTs deployment_id + hostname to /v1/domains", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { domain: { hostname: "api.example.com", status: "dns_pending" } })),
    });
    const out = await client.addDomain("d-1", "api.example.com");
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/domains");
    assert.equal(opts.method, "POST");
    assert.deepEqual(JSON.parse(opts.body), { deployment_id: "d-1", hostname: "api.example.com" });
    assert.equal(out.domain.status, "dns_pending");
  });

  it("addDomain passes the optional ingress mode (Phase 7)", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { domain: { hostname: "api.example.com", status: "active", ingress: "tunnel" } })),
    });
    const out = await client.addDomain("d-1", "api.example.com", "tunnel");
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/domains");
    assert.deepEqual(JSON.parse(opts.body), { deployment_id: "d-1", hostname: "api.example.com", ingress: "tunnel" });
    assert.equal(out.domain.ingress, "tunnel");
  });

  it("listDomains GETs /v1/domains?deployment_id=", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { domains: [] })),
    });
    await client.listDomains("d-1");
    const { url, opts } = calls[0];
    assert.ok(url.includes("/v1/domains?"));
    assert.ok(url.includes("deployment_id=d-1"));
    assert.equal(opts.method, "GET");
  });

  it("removeDomain DELETEs /v1/domains with a JSON body", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { removed: "api.example.com" })),
    });
    await client.removeDomain("d-1", "api.example.com");
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/domains");
    assert.equal(opts.method, "DELETE");
    assert.deepEqual(JSON.parse(opts.body), { deployment_id: "d-1", hostname: "api.example.com" });
  });

  it("getDomain GETs /v1/domains/:hostname and returns the lifecycle row", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, {
        domain: { hostname: "api.example.com", status: "active", dns_configured: true, tunnel_configured: true },
      })),
    });
    const out = await client.getDomain("api.example.com");
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/domains/api.example.com");
    assert.equal(opts.method, "GET");
    assert.equal(out.domain.status, "active");
    assert.equal(out.domain.tunnel_configured, true);
  });
});

describe("getLogs", () => {
  it("creates a logs task and polls it to terminal, returning the log text", async () => {
    const states = [
      { task: { id: "t-9", status: "running", result: { logs: "line1\n" } } },
      { task: { id: "t-9", status: "completed", result: { logs: "line1\nline2\n" } } },
    ];
    let polls = 0;
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        if (url === "https://cp.example.com/v1/tasks" && opts.method === "POST") {
          assert.deepEqual(JSON.parse(opts.body).payload, { deployment_id: "d-1" });
          assert.equal(JSON.parse(opts.body).type, "logs");
          return jsonResponse(201, { task: { id: "t-9", status: "queued" } });
        }
        if (url === "https://cp.example.com/v1/tasks/t-9")
          return jsonResponse(200, states[Math.min(polls++, states.length - 1)]);
        throw new Error(`unexpected ${url}`);
      },
    });
    const logs = await client.getLogs({ deploymentId: "d-1", pollIntervalMs: 1 });
    assert.equal(logs, "line1\nline2\n");
    assert.ok(polls >= 2);
  });

  it("polls an existing task id directly", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, {
        task: { id: "t-7", status: "completed", result: { logs: "boot ok" } },
      })),
    });
    const logs = await client.getLogs({ taskId: "t-7", pollIntervalMs: 1 });
    assert.equal(logs, "boot ok");
    assert.equal(calls[0].url, "https://cp.example.com/v1/tasks/t-7");
  });

  it("requires deploymentId or taskId", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, {})),
    });
    await assert.rejects(() => client.getLogs(), TypeError);
  });

  it("follow mode yields incremental chunks until the task terminates", async () => {
    const states = [
      { task: { id: "t-8", status: "running", result: { logs: "a" } } },
      { task: { id: "t-8", status: "running", result: { logs: "ab" } } },
      { task: { id: "t-8", status: "completed", result: { logs: "abc" } } },
    ];
    let polls = 0;
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url) => {
        if (url === "https://cp.example.com/v1/tasks/t-8")
          return jsonResponse(200, states[Math.min(polls++, states.length - 1)]);
        throw new Error(`unexpected ${url}`);
      },
    });
    const chunks = [];
    for await (const chunk of await client.getLogs({ taskId: "t-8", follow: true, pollIntervalMs: 1 }))
      chunks.push(chunk);
    assert.deepEqual(chunks, ["a", "b", "c"]);
  });

  it("times out when the task never reaches a terminal state", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { task: { id: "t-6", status: "running", result: {} } })),
    });
    await assert.rejects(
      () => client.getLogs({ taskId: "t-6", pollIntervalMs: 1, timeoutMs: 10 }),
      (err) => err instanceof UahtError && err.code === "timeout"
    );
  });
});

describe("initArtifact basename", () => {
  it("resolves Windows-style paths (backslashes) to a bare filename", async () => {
    const { mkdtempSync, writeFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const { tmpdir } = await import("node:os");
    // On POSIX a backslash is a legal filename char, so this exercises the
    // split-on-both-separators logic with a real stat-able file.
    const dir = mkdtempSync(join(tmpdir(), "uaht-"));
    const weird = join(dir, "some\\dir\\app.tar.gz");
    writeFileSync(weird, "bytes");
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(201, { artifact: { id: "a1" }, upload_url: "/v1/artifacts/a1/content" })),
    });
    await client.initArtifact({ project_id: "p1", filePath: weird, version: "1.0.0" });
    const body = JSON.parse(calls[0].opts.body);
    assert.equal(body.filename, "app.tar.gz");
  });
});

describe("streamEvents query params", () => {
  it("appends since and limit to the stream URL", async () => {
    const stream = new ReadableStream({ start(c) { c.close(); } });
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        return { ok: true, status: 200, body: stream };
      },
    });
    for await (const _ of client.streamEvents({ since: "2026-10-05T00:00:00Z", limit: 25 })) { /* drain */ }
    const { url, opts } = calls[0];
    assert.ok(url.startsWith("https://cp.example.com/v1/events/stream?"));
    assert.ok(url.includes("since=2026-10-05T00%3A00%3A00Z"));
    assert.ok(url.includes("limit=25"));
    assert.equal(opts.headers.Accept, "text/event-stream");
  });

  it("omits the query string when since/limit are not given", async () => {
    const stream = new ReadableStream({ start(c) { c.close(); } });
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url) => {
        assert.equal(url, "https://cp.example.com/v1/events/stream");
        return { ok: true, status: 200, body: stream };
      },
    });
    for await (const _ of client.streamEvents()) { /* drain */ }
  });
});

describe("JS/Python parity (Phase 8)", () => {
  // Canonical agent-facing method table. The Python SDK must expose the
  // snake_case twin of every entry; test_client.py asserts the same table.
  // Add new API methods to BOTH clients and extend this table.
  const PARITY_METHODS = [
    "registerAgent", "me", "rotateKey",
    "createTask", "getTask", "listTasks", "cancelTask", "approveTask", "rejectTask",
    "getLogs", "tailLogs",
    "listHosts", "getHost",
    "createProject", "listProjects", "getProject", "updateProject",
    "listArtifacts", "getArtifact", "initArtifact", "uploadArtifact", "downloadArtifact",
    "createDeployment", "getDeployment", "listDeployments", "rollbackDeployment",
    "listServices", "restartService", "stopService", "startService",
    "setSecret", "listSecrets", "deleteSecret",
    "addDomain", "listDomains", "getDomain", "removeDomain",
    "listEvents", "streamEvents",
    "deploy", "health",
  ];

  it("exposes every parity-table method", () => {
    const client = new UahtClient({ baseUrl: "https://cp.example.com", apiKey: "k" });
    const missing = PARITY_METHODS.filter((m) => typeof client[m] !== "function");
    assert.deepEqual(missing, [], `methods missing from the JS SDK: ${missing.join(", ")}`);
  });

  it("rotateKey posts to /agents/me/rotate and adopts the new key", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(jsonResponse(200, { api_key: "new-key" })),
    });
    const res = await client.rotateKey();
    assert.equal(calls.length, 1);
    const { url, opts } = calls[0];
    assert.equal(url, "https://cp.example.com/v1/agents/me/rotate");
    assert.equal(opts.method, "POST");
    assert.equal(opts.headers.Authorization, "Bearer k");
    assert.deepEqual(res, { api_key: "new-key" });
    // the server invalidates the old key immediately, so the client must
    // adopt the new one or every later call breaks
    assert.equal(client.apiKey, "new-key");
  });

  it("tailLogs yields incremental chunks until the logs task terminates", async () => {
    const states = [
      { task: { id: "t-8", status: "running", result: { logs: "a" } } },
      { task: { id: "t-8", status: "running", result: { logs: "ab" } } },
      { task: { id: "t-8", status: "completed", result: { logs: "abc" } } },
    ];
    let idx = 0;
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        assert.ok(url.endsWith("/v1/tasks/t-8"));
        const body = states[Math.min(idx, states.length - 1)];
        idx += 1;
        return jsonResponse(200, body);
      },
    });
    const chunks = [];
    for await (const chunk of client.tailLogs({ taskId: "t-8", pollIntervalMs: 1 })) {
      chunks.push(chunk);
    }
    assert.deepEqual(chunks, ["a", "b", "c"]);
  });
});

describe("rotateKey error paths", () => {
  it("surfaces a 403 as UahtError with the protocol code and keeps the old key", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(
        jsonResponse(403, { error: { code: "forbidden", message: "missing or invalid bearer token" } })
      ),
    });
    await assert.rejects(() => client.rotateKey(), (err) => {
      assert.ok(err instanceof UahtError);
      assert.equal(err.code, "forbidden");
      assert.equal(err.status, 403);
      return true;
    });
    // a failed rotation must not clobber the working key
    assert.equal(client.apiKey, "k");
  });

  it("surfaces permission-denied errors with code, status, and message", async () => {
    const client = new UahtClient({
      baseUrl: "https://cp.example.com",
      apiKey: "k",
      fetch: mockFetch(
        jsonResponse(403, { error: { code: "forbidden", message: "missing permission: deploy" } })
      ),
    });
    await assert.rejects(() => client.createDeployment({ projectId: "p", version: "v1" }), (err) => {
      assert.ok(err instanceof UahtError);
      assert.equal(err.code, "forbidden");
      assert.equal(err.status, 403);
      assert.equal(err.message, "missing permission: deploy");
      return true;
    });
  });
});
