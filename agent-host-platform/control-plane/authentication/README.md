# authentication (module map)

Implemented in `api/src/middleware/auth.ts` + `api/src/lib/tokens.ts` + `api/src/lib/secrets.ts`.

- Two credential kinds: agent API keys and host tokens, both `Authorization: Bearer`.
- Tokens stored as SHA-256 hashes; constant-time comparison.
- Scoped permissions per agent (`deploy`, `read_status`, `read_logs`, `restart`,
  `stop`, `remove`, `manage_domains`, `approve_deployments`, `manage_secrets`).
- Project secrets encrypted at rest with AES-256-GCM (`DATA_ENCRYPTION_KEY`).
- See `agent-sdk/protocol/PROTOCOL.md` §1 for the contract.
