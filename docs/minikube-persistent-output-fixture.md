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

The Job mounts only the supplied PVC at the supplied output mount, requires that
mount to be empty, writes exactly `hello.txt` containing `hello world` followed
by a newline, and prints it in the Job log. On success the script reports the
human-readable PVC path, for example `PVC fixture-output:/persistent-output/hello.txt`.

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
