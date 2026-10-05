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
