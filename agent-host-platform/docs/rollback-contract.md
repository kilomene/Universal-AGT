# Rollback result contract — Universal AGT

## Success is health-proven, never inferred

A rollback is successful **only** when the worker's report proves all
three:

```
status               = 'rolled_back'
AND target_health_status = 'healthy'
AND rollback_status      = 'succeeded'
```

Only this state transitions the deployment to `rolled_back`.

**Anything missing or contradictory → `rollback_failed`.** In
particular:

| report | result |
|---|---|
| `{"status": "rolled_back", "target_health_status": "healthy", "rollback_status": "succeeded"}` | `rolled_back` |
| `{"status": "rolled_back"}` | `rollback_failed` |
| `{"status": "rolled_back", "target_health_status": "unknown"}` | `rollback_failed` |
| `{"status": "rolled_back", "target_health_status": "unhealthy"}` | `rollback_failed` |
| `{"status": "rolled_back", "target_health_status": "healthy", "rollback_status": "failed"}` | `rollback_failed` |
| `{"status": "rolled_back", "target_health_status": "healthy"}` (no `rollback_status`) | `rollback_failed` |
| `{"status": "ok"}` (legacy) | `rollback_failed` |
| `{}` (empty) | `rollback_failed` |

Success is never inferred from missing fields. The control plane is the
trust boundary here — the worker is untrusted input, and the backstop in
`POST /v1/worker/tasks/:id/progress` (`mirrorDeploymentState`) enforces
the contract independently of worker behavior.

The `deployment.rollback_failed` event carries `target_health_status`,
`rollback_status`, and a plain-text `reason` (status strings only, never
secrets), e.g. `target health could not be verified`.

## State machine

```
rollback_requested
        ↓
target_verified        (exists, same project/host, restorable:
                        image/artifact/state present)
        ↓
current_removed        (verify-before-destroy: the healthy current
                        deployment is untouched until the target is
                        proven restorable)
        ↓
target_restored
        ↓
health_check
        ↓
     ┌───────────────┐
     │               │
  healthy      unhealthy / unknown
     │               │
     ↓               ↓
settle_ports    rollback_failed
     │
     ↓
rolled_back
```

- Port settlement runs **only after** the target is proven healthy.
  A settle failure after a healthy restore persists `rollback_failed`
  (`rollback_status = 'partially_reconciled'`) with the settle outcome
  kept on the row for reconciliation/retry — never a false success.
- Same-port rollback (V1 and V2 on `:8080`) is supported deliberately:
  stop/remove V2 → restore V1 → health-check V1 → settle `:8080` →
  `rolled_back`. Two containers never share a host port.
- A third-party process already on the target port is never terminated
  or taken over to complete a rollback — that is a durable
  `rollback_failed`.
- Garbage collection only runs after successful state settlement; a
  rollback target is never collected while a rollback may need it.
- Both automatic (health-check failure) and explicit
  (`POST /v1/deployments/:id/rollback`) rollbacks execute the single
  shared implementation (`host-worker/deployments/rollback.py`).

## Rollback targets and the `superseded` state

The rollback target is the newest deployment of the same project+host
(excluding the current one) that is `running` **or `superseded`** and
`healthy` (`selectRollbackTarget`). `superseded` is the normal case:
when V2 became healthy, V1 was moved there (migration 015) precisely so
it stays available as a rollback target without holding a resource
reservation.

On a successful rollback (the triple-check above), the control plane
does two writes in one transaction:

```
V2 (the rolled-back deployment)  → rolled_back   (terminal; reservation released)
V1 (the restored target)         → running       (reservation re-acquired)
```

The restored id is the control plane's own choice from task creation
(`payload.target_deployment_id`), falling back to the worker's
`rolled_back_to` for hand-built tasks; either way it is guarded to the
same project+host and only a `superseded` row is eligible, so a
confused worker report cannot flip an arbitrary deployment. After the
restore, worker and control plane agree again: V1 is `running` in both.

## What `unknown` means

`target_health_status = 'unknown'` means the system restored the target
but could not establish its health (e.g. no health-checkable endpoint).
`unknown` is **not** healthy. It persists `rollback_failed` with reason
`target health could not be verified`.
