# Minikube persistent-output development fixture

`scripts/check_minikube_persistent_output.py` renders a fixed, digest-pinned Job
by default. Rendering does not invoke Minikube or Kubernetes. Supplying `--run`
is the only path that creates a Job, and every target is explicit: kubeconfig,
context, HTTPS server, CA file, namespace, execution and Minikube profiles, PVC,
output mount, cluster/namespace/PVC UIDs, the canonical SHA-256 digest of the
PVC `spec`, and the existing Job authority identity fields. Before it opens the
create journal, `--run` queries the contacted explicit server for the `kube-system`
namespace cluster anchor, selected namespace, and selected PVC. Their observed
UIDs, PVC namespace/name, and canonical PVC spec digest must match the supplied
target. The Job annotations bind that verified PVC UID/spec digest as well as its
name; supplied hashes alone are never sufficient to create a Job.

`network_mode=none` is admitted only with a caller-supplied explicit
`network-policy-enforced-v1` target capability and a named NetworkPolicy
UID/spec digest. The fixture reads that policy from the selected namespace and
requires its exact Tessera-managed-Pod selector with empty ingress and egress
rules. An annotation requesting `none` records the request; it is never
enforcement evidence.

The Job mounts only the supplied PVC at the supplied output mount, requires that
mount to be empty, writes exactly `hello.txt` containing `hello world` followed
by a newline, and prints it in the Job log. After completion the fixture validates
effective owned Pod UID/spec/image and PVC binding, then requires explicit
`create pods` and `create pods/exec` authorization for a fixed read-only
readback Pod. Only an exact post-exit readback of retained bytes is success. If
that readback is absent, unauthorized, or differs, the nonzero result reports
**narrow log smoke only**; logs never prove retained output.

It never installs, starts, switches, creates, or deletes a cluster. Missing
`kubectl`, Minikube, or Kubernetes `NotFound` prerequisites are reported as
skipped without Job creation. Authentication/authorization, connectivity,
timeout, malformed-response, and identity/spec-conflict failures are failures,
not absence; they never enter create and report only sanitized categories. Every
subprocess has a finite deadline, requests bounded server time, and accepts at
most 16 KiB of output (Job logs are limited to one 1 KiB line). `--run` returns
zero only for a verified fixture output; a skip returns exit status 2 and every
other result returns 1. Neither exit status is result acceptance, storage/quota
proof, provider execution, artifact release, or pilot evidence.

This fixture is not Minikube bootstrap or target attachment. Any future bootstrap
and attachment workflow must be separately human-authorized and bind the explicit
identity above; it must never select an ambient kube context. It performs no
automatic destructive teardown.
