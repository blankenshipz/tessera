# Stop-only recovery of an expired never-claimed worker

This path is for one human-cancelled Kubernetes dispatch that was **never claimed**.
It is not a replacement worker enrollment or permission to retry execution. The
original worker bearer remains `401`; no credentials, leases, packages or dispatches
are issued or renewed. It cannot settle possibly submitted/unknown work.

## Preconditions and proof boundary

- The **original human grant issuer**, separately authenticated using their current
  control credential, performs issuance and reconciliation in that same session.
  The exact original worker credential must be expired, not replaced or renewed.
- Stop the original submitting Deployment/process with UID/resourceVersion fencing,
  await its termination, and retain exclusive custody of its original volume and
  three journals. Record a custody reference and SHA-256 digest of the sanitized
  stop/volume/holder evidence. Do not recreate journals, restore a backup, delete
  history, use SQL cleanup, or let another worker share the workspace.
- Control must show one original grant/lease/admission/package/operation/dispatch,
  no current **or historical** claim, no observations, receipts, provider identity,
  remote route or existing operation recovery. All identities/revisions/epochs
  must match. Even unconfirmed unknown evidence prevents this proof.
- Provider reads use an **already authorized** runtime token/CA reference. This
  maintenance authorization does not mint a provider token or change RBAC. Required
  reads include actual cluster/workload namespace UIDs, bound PVC/network policy
  identity, deterministic Job GET, complete operation-labelled Job and Pod lists,
  and complete output-operation-labelled helper Pod list. All must succeed.
  A `403`, timeout, continued/incomplete list or any matching workload leaves the
  reservation held. Do not relabel denial as an empty list or skip a read.
- If the current reference lacks these reads, the authorized human cluster operator
  must explicitly provision a narrowly scoped read Role/RoleBinding for the
  approved workload namespace: Job/Pod `get,list` only, plus existing necessary
  namespace/PVC/network-policy identity reads. No `create`, `update`, `patch`,
  `delete`, `exec`, wildcard resources/verbs or cross-namespace list authority.
  This is a separate explicit human provisioning step, not a collector fallback.
  Record its resource UID as the provider-read authorization reference. If that
  provisioning is outside the operator's authorization, obtain that specific
  authorization; don't ask for/reissue the expired worker token.

The frozen declared target is never edited. The actual target must match exactly,
or differ **only** by the previously omitted network-none witness; the collector
checks this relationship. Maintenance binds the complete actual target digest,
cluster UID, namespace name and UID. Normal execution claim guards stay unchanged.

## 1. Issue narrow human maintenance authorization

Build a private JSON request from retained non-secret enrollment/dispatch receipts
and current human-authorized grant metadata. No bearer or kubeconfig contents belong
in this request. Required fields:

```text
expected_revision, grant_epoch, dispatch_id, operation_id, lease_id, epoch,
package_digest, target_digest, actual_target_digest, target_relation,
namespace_uid, job_name, original_worker_principal_id,
original_worker_credential_id, namespace, cluster_uid,
custody_reference, custody_digest, provider_read_authorization_reference,
human_attests_original_worker_stopped_exclusive_custody: true,
human_attests_existing_provider_read_authorization: true,
purpose: "stop-only-never-claimed", expires_at: ISO-8601 offset time
```

Expiry must be in the future and no more than **five minutes** away. The original
operation epoch is distinct from the current cancelled grant epoch. An existing
unexpired authorization prevents a second one; after expiry a new authorization
may preserve the same immutable custody/identity/read-reference basis, not replace
it. Keep issuance/reconciliation idempotency keys stable across transport retries.

The following command uses a private existing human header reference (file contains
the Authorization header); it never puts the bearer on the command line:

```sh
umask 077
curl --fail-with-body --silent --show-error \
  --cacert "$CONTROL_CA_REFERENCE" \
  --header "@$HUMAN_HEADER_REFERENCE" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $MAINTENANCE_ISSUE_KEY" \
  --data-binary "@$MAINTENANCE_REQUEST_FILE" \
  "$CONTROL_ORIGIN/execution-grants/$ORIGINAL_GRANT_ID/never-claimed-maintenance" \
  > "$MAINTENANCE_AUTHORIZATION_FILE"
```

The response is `{authorization, authorization_digest}`. It is **not a bearer** or
an executable package. It records the actual human issuer/maintenance session,
expired original worker identity, read-reference and custody binding. Do not give
the human control credential to the collector.

## 2. Collect proof in the stopped original workspace

Install the repaired distribution in the exclusive maintenance environment. Run
this exact supported module command with existing approved provider-read references:

```sh
umask 077
python -m tessera_worker.kubernetes_queued_recovery \
  --target-file "$ORIGINAL_TARGET_FILE" \
  --ca-file "$KUBERNETES_CA_REFERENCE" \
  --kubernetes-token-file "$AUTHORIZED_PROVIDER_READ_TOKEN_REFERENCE" \
  --workspace "$ORIGINAL_WORKSPACE" \
  --maintenance-file "$MAINTENANCE_AUTHORIZATION_FILE" \
  --exclusive-stopped-worker \
  > "$MAINTENANCE_PROOF_FILE"
```

There is no worker bearer argument. The collector rejects foreign/expired
authorization or target before provider reads, checks intact scanner history and
absence of all local effect intents, commits insert-denying fences on the original
Job/adapter/descriptor/helper journals, then performs actual provider reads. These
fences stay in place even if reads fail. It creates no Job, helper or replacement
execution binding. A failed command yields **no usable proof**; do not submit its
empty output file. The emitted payload is `{proof, proof_digest}` with normalized
timestamps and actual human maintenance attribution. It is self-reported evidence,
not an independently signed Kubernetes receipt.

## 3. Human consumption and once-only release

Within **30 seconds of the start of the provider reads**, and before maintenance
expiry, the same authenticated human issuer submits the emitted payload:

```sh
curl --fail-with-body --silent --show-error \
  --cacert "$CONTROL_CA_REFERENCE" \
  --header "@$HUMAN_HEADER_REFERENCE" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $MAINTENANCE_RELEASE_KEY" \
  --data-binary "@$MAINTENANCE_PROOF_FILE" \
  "$CONTROL_ORIGIN/execution-grants/$ORIGINAL_GRANT_ID/reconcile-maintenance-never-claimed" \
  > "$MAINTENANCE_RELEASE_RECEIPT_FILE"
```

Control checks exact authorization/proof digests, actual human actor/session,
expired original identity, custody, provider-read reference, target and all
transactional no-claim/no-effect history again. A successful response settles zero
once and appends operational `interrupted`/`not_executed` events; all old history is
retained. Cancellation is **not** confirmed termination and no research conclusion
is fabricated. Same-key replay returns the bookkeeping receipt, not execution
authority; a changed key cannot release twice. Human experiment export joins the
distinct maintenance events as redacted opaque references, preserving notebook
completeness and execution-not-established semantics.

## Current live evidence (not acceptance)

The initial installed 2026-10-04 attempt encountered an actual operation Job-list
`403`, then the original bearer returned `401` after its 21:14:03Z expiry. The
original worker was stopped and its journals/workspace retained; that receipt had
no complete proof, attestation, release, Job/output success or termination claim.
It remains preserved as local evidence ID
`REMOTE-PLATFORM-QUEUED-EXPIRY-BLOCKED-2026-10-04`.

A later bounded maintenance run superseded that blocker, not the historical
receipt. The separately authorized operator provisioned only namespace-UID-scoped
Job/Pod `get,list` for the existing dispatch service account (no cluster-wide RBAC
change). Human issuance returned **201** and proof consumption/reconciliation
returned **200**, with exact same-key replay; the expired worker was not renewed,
no replacement attestation was substituted, and provider termination was not
inferred. The runbook flow preserved the stopped original workspace and retained
its histories. See local evidence ID
`REMOTE-PLATFORM-MAINTENANCE-RUN5-2026-10-04`.

This proves only the original never-claimed recovery/cancellation accounting path.
The distinct run-5 experiment completed a Kubernetes Job and collected a real
36-byte output, but its lease had been cancelled: output remained quarantined and
the supported human outcome was `cancelled`, not success. Its final notebook
export remains metadata-only/`partial_page`, without output-manifest correlation;
the critic outbox is unverified. Dedicated reasoning OAuth admission and
independent-host/laptop-off proof remain separate.
