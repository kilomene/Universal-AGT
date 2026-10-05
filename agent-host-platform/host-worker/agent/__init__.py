"""Host worker agent package: config, control-plane API client, task policy,
and the main loop. The worker is OUTBOUND ONLY — it opens HTTPS connections
to the control plane and never listens on any port."""
