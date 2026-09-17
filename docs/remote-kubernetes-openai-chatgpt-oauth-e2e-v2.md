# Astra v2 ten-turn remote Kubernetes OAuth E2E

`scripts/check_remote_kubernetes_openai_chatgpt_oauth_e2e_v2.py` is an opt-in,
retained operational fixture for
`remote-kubernetes-openai-chatgpt-oauth-e2e-v2`. It is not collected by pytest
or CI. It is provider-ready metadata and a runner contract only: it does not
launch a provider, resolve credentials, provision or modify a cluster, create a
Secret, change a global guard, or make a paid call by itself.

The existing v1 one-call fixture remains separate and unchanged.

## Preconditions

Preflight requires an absolute regular kubeconfig, explicit context and existing
namespace; `kubectl`; permission to server-dry-run and create Jobs; and an
already-ready runner Deployment and ServiceAccount. The Deployment must use the
exact requested `repository@sha256:...` image and expose annotations for this
v2 profile, the owner ID, the SHA-256 of the opaque OAuth runtime reference, and
remote-workflow protocol `v43`.

The runtime reference is exactly `openai-chatgpt-oauth://OWNER/REFERENCE`; it is
not an API key or bearer token. The remote runner must already be able to resolve
it through an authorized bridge. It must implement `run-ten-turns`, use one
durable conversation and one invocation, make exactly ten sequential model calls
and exactly one `read_experiment_status` call per turn, and emit a final JSON
attestation. Shell, MCP, skills, arbitrary tools, automatic retries, fallback,
API keys, and top-up are all explicitly disabled in the manifest.

Each turn is bounded by `--max-turn-tokens` and `--max-turn-seconds`; the ten
turns are bounded by `--max-total-tokens`, ten model calls, and `--timeout`.
Attestation must identify the same operation, conversation, invocation, actual
owner Job UID, and Pod UID. The downward-API Pod identity is never relabelled
as a Job UID: the fixture derives the Job UID from that Pod's controller owner
reference and rejects a different owner or replacement Pod on reconnect.
Attestation must provide known structured per-turn and aggregate usage; and declare
`chatgpt_oauth_subscription_only`. Unknown usage, a malformed bound, an identity
mismatch, a failed Job, or a lost create/submit/report/post-submit observation is
recorded as `reconciliation_required`. The script never retries or auto-resubmits.

## Explicit two-step operation

First run preflight only and retain its manifest digest:

```sh
.venv/bin/python scripts/check_remote_kubernetes_openai_chatgpt_oauth_e2e_v2.py \
  --preflight --kubeconfig /absolute/path/remote-kubeconfig --context remote-e2e \
  --namespace tessera-e2e --runner-deployment oauth-runner \
  --service-account tessera-live \
  --image registry.example/tessera-runner@sha256:REPLACE_WITH_64_HEX \
  --oauth-owner-id OWNER --oauth-runtime-ref openai-chatgpt-oauth://OWNER/OPAQUE_REFERENCE \
  --model MODEL --operation-id 32_LOWERCASE_HEX_CHARACTERS \
  --journal /private/path/tessera-live-oauth-v2.sqlite3
```

After a human reviews that exact digest and authorizes ten subscription OAuth
model calls, repeat the arguments with:

```sh
  --run --confirm-manifest-digest MANIFEST_DIGEST_FROM_PREFLIGHT \
  --confirm-provider-calls TEN_OAUTH_MODEL_CALLS:MANIFEST_DIGEST_FROM_PREFLIGHT
```

The journal atomically persists ten planned turn records, a submit intent, a
subscription-only reservation and consumes confirmation before it checks for or
creates a Job. Confirmation is one-use, including after a restart. An existing
operation Job is a reconciliation condition, not an invitation to submit another
Job; duplicate checks use only `kubectl get`.

During the configured local observation pause, the journal records
`local_observation_paused`, not remote progress. On reconnect it records a
separate Kubernetes `remote_progress` observation only after proving the same
operation, invocation and Job UID. The Job and private journal are retained.
`--cancel` only records `cancel_requested`; it never attempts provider or Job
termination, which remains an explicit reconciliation workflow.

## Live prerequisites and limitation

This repository has no enrolled v2 runner image, remote credential bridge,
authorized OAuth subscription, cluster target, deployment, or operator
confirmation. Consequently no live/provider run is implied or performed by this
change. The local journal is forensic fixture evidence, not control-plane
authority, provider billing settlement, result acceptance, or scientific proof.
