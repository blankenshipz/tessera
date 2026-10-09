# Astra namespace lifecycle

`tessera astra-namespace` is a narrow, operator-only boundary for an explicitly
named Kubernetes namespace. It is not Astra control-plane installation and does
not attach a target, enroll a worker, deliver credentials, or dispatch work.

`inspect-existing` and `plan-use-existing` issue only named `kubectl get
namespace` calls. Choosing an existing namespace performs zero writes. A
dedicated namespace needs a fresh absent inspection, an exact plan digest approved
before its expiry, and the explicit `create` command. Creation submits exactly one
generated `Namespace`, with `managed-by=tessera`, installation, and operation
labels. Its receipt records the returned UID and resourceVersion. Successful status
is deliberately **`namespace_created_control_plane_not_installed`**.
Use `create --receipt-output /absolute/private/file` to retain that receipt for
the later read-only `status` or `reconcile` command.

If create returns a timeout, duplicate, malformed response, or any uncertain
result, Tessera returns `reconciliation_required`; it never retries or adopts a
same-named namespace. `status`/`reconcile` are read-only and require the original
receipt to recognize a created namespace, fencing the target UID and labels. There
is no cleanup command: deletion remains disabled by default and would require a
separate, UID/ownership-fenced decision.

The operator rejects ambient contexts, insecure TLS, missing CA identity, Secret
reads, response kind/name/namespace mismatches, and oversized output. Kubernetes
is not contacted by the default test suite; the CLI `create` action is the only
mutation entry point and must be run by an operator against an explicitly chosen
target.
