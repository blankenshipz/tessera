# Deterministic remote coverage

Default tests use temporary SQLite state and synthetic workers, harnesses, and
providers. They cover project-scoped command admission, revision/idempotency
replay (including a lost response), durable event polling (`after` is an
exclusive cursor), concurrent readers, event/state rollback, worker lifecycle
fences, remote workflow checkpoints/reconnect/reconciliation, and
provider-disabled coordinator/context-delivery boundaries. Renderer tests fence
late connection, project, context, and remote-workflow replies.

The labbook is a bounded polling stream, not an SSE endpoint. Upstream harness
SSE ordering, duplicates, gaps, stale events, reconnect cursors, and untrusted
event quarantine are tested through the synthetic OpenCode adapter fixture.
These tests establish control-plane record behavior only: a context delivery,
answer, stop request, heartbeat, or exited observation is not evidence,
scientific acceptance, or confirmed provider execution unless the applicable
attributed record says so.

Docker, Kubernetes, remote OAuth, and provider harness scripts under `scripts/`
remain explicit opt-in fixtures. They are not part of default pytest or desktop
CI, and passing synthetic coverage is not live execution evidence.
