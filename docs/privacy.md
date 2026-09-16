# Privacy and local data handling

Tessera is currently a source-only developer alpha. It has no Tessera-operated
hosted service and no published production privacy program.

The local control service stores its state in the directory selected at
initialization (commonly `.tessera`). That state can include research records,
labbook history, retained SQLite BLOBs, metadata, and local credentials such as
the owner header. Treat the directory, backups, exported JSON, and terminal
output as sensitive unless you have independently classified them otherwise.

Current limits:

- The developer workflow does not provide organization account recovery,
  centralized deletion, retention scheduling, or a hosted support channel.
- Backup is a local SQLite snapshot plus a versioned object bundle containing
  committed local project bytes and custody metadata. Missing or unsafe required
  objects fail closed; SQLite-only legacy artifacts remain in the snapshot. See
  [recovery](recovery.md).
- Restore copies bundled objects into a permanently read-only held history with a
  fresh local owner credential. It does not stop or fence the original service,
  workers, or external jobs; operational failover is unsupported.
- Synthetic OpenCode and fake-provider fixtures do not use real provider
  credentials. No real OpenCode/provider integration is supported or claimed.

Before using local state with real or sensitive data, independently determine
whether the storage location, access controls, backup location, retention, and
applicable obligations are suitable. Do not put secrets or sensitive research
data in issues, pull requests, examples, or diagnostics.
