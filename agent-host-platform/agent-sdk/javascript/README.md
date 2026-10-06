# uaht-sdk

JavaScript (Node 18+) SDK for the Universal Agent-to-Persistent-Host Deployment
System control plane — wire protocol v1. Zero dependencies.

## Install

Copy this package into your project (or link it locally); there is nothing to
install.

## Usage

```js
import { UahtClient, UahtError } from "uaht-sdk";

const client = new UahtClient({
  baseUrl: process.env.UAHT_BASE_URL, // e.g. https://cp.example.com
  apiKey: process.env.UAHT_API_KEY,   // your agent API key
});

try {
  const me = await client.me();
  console.log("agent:", me.agent.name);
} catch (err) {
  if (err instanceof UahtError) {
    console.error(`control plane error [${err.code}]: ${err.message}`);
  }
  throw err;
}
```

## Deploy helper

```js
// Upload the artifact first, then deploy.
const { artifact, upload_url } = await client.initArtifact({
  project_id: "proj-uuid",
  filePath: "./dist/app.tar.gz",
  version: "1.2.0",
});
await client.uploadArtifact(upload_url, "./dist/app.tar.gz");

const { deployment, task } = await client.deploy({
  project: "my-api",       // project name (or id)
  host: "host-01",         // host name (or id); omit for any capable host
  version: "1.2.0",
  artifactId: artifact.id,
  mode: "automatic",
  wait: true,              // poll until the task completes
});
console.log(deployment.status, task.status);
```

## Secrets

```js
await client.setSecret("proj-uuid", "DB_PASSWORD", "..."); // encrypted at rest, never returned
const names = await client.listSecrets("proj-uuid");       // names only
await client.deleteSecret("proj-uuid", "DB_PASSWORD");
```

## Live event stream

```js
for await (const event of client.streamEvents({ since: "2026-10-05T00:00:00Z", limit: 50 })) {
  console.log(event.type, event.deployment_id || event.task_id);
}
```

Pass an `AbortSignal` via `{ signal }` to close the stream from the consumer.
The server replays the last `limit` events before live-pushing.

## Logs

```js
// Create a `logs` task for a deployment and poll it to terminal.
const text = await client.getLogs({ deploymentId: "dep-uuid" });

// Or poll an existing logs task by id, and stream chunks as they arrive:
for await (const chunk of await client.getLogs({ taskId: "task-uuid", follow: true })) {
  process.stdout.write(chunk);
}

// Async generator of chunks, no final text:
for await (const chunk of client.tailLogs({ deploymentId: "dep-uuid" })) {
  process.stdout.write(chunk);
}
```

## Domains & key rotation

```js
await client.addDomain(deploymentId, "api.example.com", "tunnel"); // 'tunnel'|'direct', omit for server default
await client.listDomains(deploymentId);
await client.removeDomain(deploymentId, "api.example.com");
const { api_key } = await client.rotateKey(); // new key shown once, client adopts it
```

## Error shape

Every failure throws `UahtError` with `{ code, message, status, body }`,
mirroring the protocol error shape `{ error: { code, message } }`.

## Registration & token rotation

```js
// Register a new agent: the control plane gates this on the provisioning
// token (X-Provisioning-Token header) — no agent key needed yet.
const bootstrap = new UahtClient({ baseUrl: process.env.UAHT_BASE_URL });
const reg = await bootstrap.registerAgent({
  name: "ci-bot",
  type: "ci",
  permissions: { deploy: true, read_status: true },
  provisioningToken: process.env.UAHT_PROVISIONING_TOKEN,
});
const apiKey = reg.api_key; // shown once — store it, it is never returned again

// Rotate your own agent key (the client adopts the new key automatically).
await client.rotateKey();

// Register a host (provisioning token, or a deploy-permission agent key).
const host = await client.registerHost({
  name: "edge-01",
  provisioningToken: process.env.UAHT_PROVISIONING_TOKEN,
});
const hostToken = host.host_token; // shown once

// Rotate a host's token (auth is the host token itself; the client adopts it).
const hostClient = new UahtClient({ baseUrl: process.env.UAHT_BASE_URL, apiKey: hostToken });
await hostClient.rotateHostToken(host.host.id, { graceSeconds: 300 });
```

## Tests

```sh
npm test   # node --test test/*.test.js
```
