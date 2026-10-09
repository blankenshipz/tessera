# 0026 — Protected native enrollment without remote custody fiction

Status: implemented for local native macOS custody; provider activation unchanged.

Extend decision 0025's metadata-only administration with an explicit OS-backed
custody boundary. API and renderer receive only scope, opaque reference/version,
review, lifecycle and redacted audit metadata. Values enter a one-shot native
helper through a hidden OS prompt or private inherited descriptor; no HTTP value
route, generic IPC read, argv/env credential or plaintext fallback exists.

Native macOS Security.framework is the available implementation. Windows Credential
Manager and Linux Secret Service adapters explicitly report unavailable. A test-only
injected memory fake is visibly non-production and never selected automatically.

The root reviewer and original project-human proposer must differ and remain live
at enrollment. A five-minute server-minted approval and expected revision bind a
durable one-use claim. Store effects occur only after current desktop context is
reconfirmed; final authority is rechecked under the database write lock. OS write
and SQLite cannot share an atomic transaction: crash/uncertainty leaves an unusable
claim and explicit cleanup, never a fabricated success or replay. Rotation fences
old versions; revoke/expire/tombstone and physical purge remain distinguishable.

Supported desktop topology is the native local control database on the same host
as the helper/Keychain. Both local serving profile and the exact approval's presence
in that database are required; loopback alone is not locality. Remote Kubernetes
needs its own separately authenticated broker/secrets service, not access to the
laptop's Keychain. That remote handoff is unimplemented and fails closed.

No new service identity or runtime secret delegation is inferred. The in-process
trusted-server resolver requires per-use server admission; it has no API/IPC binding
and is not wired to the still-disabled Salad live entry point. General administration
delegation does not grant enrollment, rotation, approval or secret read authority.

See [runbook and threat model](../protected-secret-enrollment.md) for memory/ACL
limitations, supported encodings, backup separation, incident response and the
remaining production/native-conformance gates. Existing fake paths stay synthetic;
enrollment is not provider activation, provider-key validity or conformance evidence.
