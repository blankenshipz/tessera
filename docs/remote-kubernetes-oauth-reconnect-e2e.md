# Remote Kubernetes OAuth reconnect E2E

`scripts/check_remote_kubernetes_oauth_reconnect_e2e.py` is an opt-in, retained
operational fixture. It is not collected by pytest or CI, and it does not promote
the current provider-disabled Kubernetes profile to a supported provider profile.

The fixture requires all of the following before its **preflight** can pass:

- `kubectl`, an absolute explicit kubeconfig, explicit context, and an existing
  remote namespace. It never selects the current context.
- An existing ready runner Deployment and ServiceAccount in that namespace. The
  Deployment must use the exact requested `repository@sha256:...` image and have
  these annotations: `tessera.dev/live-profile=remote-kubernetes-openai-chatgpt-oauth-e2e-v1`,
  `tessera.dev/openai-chatgpt-oauth-owner`, and
  `tessera.dev/openai-chatgpt-oauth-runtime-ref-sha256`, plus
  `tessera.dev/remote-workflow-protocol=v43`.
- An opaque, owner-bound runtime reference in the form
  `openai-chatgpt-oauth://OWNER/REFERENCE`. It is a reference, not a bearer token
  or API key. The runner Deployment's reference hash must match it.
- A runner image implementing `/tessera-live-oauth-e2e run`. It must resolve that
  reference through the already-authorized remote credential bridge, make at most
  one call with the requested model, hold for the requested disconnect interval,
  and emit a final JSON attestation containing the operation id, model,
  `model_calls: 1`, `provider: "openai-chatgpt-oauth"`, `usage`, and false
  `api_key_used`, `fallback_used`, and `top_up_used` fields. The runner must also
  identify its remote workflow protocol as `v43`, emit its first checkpoint, and
  report `control_continued: true` and `same_operation_reconnected: true` after
  reconnecting to the original v43 operation through its already-configured remote
  control transport. The local script never receives or prints that worker control
  credential.

No API-key argument, environment fallback, automatic account top-up, Secret
creation, deployment creation, namespace deletion, or automatic cleanup exists in
this script. A missing kubeconfig, namespace, deployment, target permission,
image evidence, or runtime-reference binding stops before a Job is submitted.

First run the non-provider preflight. Save the printed `manifest_digest`; the local
SQLite journal contains separate `live_profile_runs`, `live_profile_usage`, and
`live_profile_reconciliation` evidence and does not reuse or relabel
provider-disabled records.

```sh
.venv/bin/python scripts/check_remote_kubernetes_oauth_reconnect_e2e.py \
  --preflight \
  --kubeconfig /absolute/path/to/remote-kubeconfig \
  --context remote-e2e \
  --namespace tessera-e2e \
  --runner-deployment oauth-runner \
  --service-account tessera-live \
  --image registry.example/tessera-runner@sha256:REPLACE_WITH_64_HEX \
  --oauth-owner-id OWNER \
  --oauth-runtime-ref openai-chatgpt-oauth://OWNER/OPAQUE_REFERENCE \
  --model MODEL \
  --operation-id 32_LOWERCASE_HEX_CHARACTERS \
  --journal /private/path/tessera-live-oauth-e2e.sqlite3
```

Only after reviewing the preflight output and manifest digest, a human explicitly
confirms the one paid/provider effect. The second invocation repeats the same
arguments and adds:

```sh
  --run \
  --confirm-manifest-digest MANIFEST_DIGEST_FROM_PREFLIGHT \
  --confirm-provider-call ONE_OAUTH_MODEL_CALL:MANIFEST_DIGEST_FROM_PREFLIGHT
```

The run creates one Job, observes it, intentionally drops the local desktop/client
observation interval while the remote Job continues, reconnects to the same Job
UID, and verifies that a second create of the same operation is rejected with
`AlreadyExists`. It retains the Job and journal. A timeout, failed Job, malformed
attestation, or uncertain observation is recorded as reconciliation-required; the
fixture never submits a replacement Job.

The remote worker, not the local desktop client, owns the existing v43 heartbeat,
checkpoint, and reconnect calls through its configured control transport. The local
script verifies its bounded attestation and the same Kubernetes Job UID; it does not
receive a worker control credential or independently turn a runner assertion into
result acceptance or proof of provider billing.
