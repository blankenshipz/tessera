# 0007: Credential-access leases are metadata-only and separate from authentication

Schema v21 records human-created provider-neutral credential-access leases without
selecting a secrets vendor. A lease binds an opaque reference, audience, purpose and
scope to one current organization/project/task/grant/execution-lease/worker/epoch
tuple. It can neither outlive the parent execution lease nor be created or renewed
by a worker.

The control API returns a short-lived opaque fake-backend handle to the bound worker
after live tenant, credential, registry, grant, lease and epoch checks. The private
in-memory backend is a test/development seam only; its `put`, `resolve`, and
`revoke` operations retain values only in process memory and its access log has no
values. Each handle expires and is invalidated independently: expiry or disposal
of one handle does not revoke the durable lease or sibling handles. Resolve
rechecks current authority, so grant cancellation, access-lease expiry, worker
retirement, epoch fencing, and explicit lease revocation deny every affected
handle and block future issuance.

No production secrets backend, provider credential injection, command-line
credential path, or real provider operation integration is authorized by this
decision. Operation records may later retain only an opaque credential-access lease
ID when necessary; raw secret values and references are excluded from receipts and
diagnostics.
