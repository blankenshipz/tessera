# Minikube persistent-output development fixture

`scripts/check_minikube_persistent_output.py` renders a fixed, digest-pinned Job
by default. Rendering does not invoke Minikube or Kubernetes. Supplying `--run`
is the only path that creates a Job, and every target is explicit: kubeconfig,
context, HTTPS server, CA file, namespace, execution and Minikube profiles, PVC,
output mount, and the existing Job authority identity fields.

The Job mounts only the supplied PVC at the supplied output mount, requires that
mount to be empty, writes exactly `hello.txt` containing `hello world` followed
by a newline, and prints it in the Job log. On success the script reports the
human-readable PVC path, for example `PVC fixture-output:/persistent-output/hello.txt`.

It never installs, starts, switches, creates, or deletes a cluster. Missing
`kubectl`, Minikube, or PVC prerequisites are reported as skipped without Job
creation. This is not storage/quota proof, provider execution, artifact release,
result acceptance, or pilot evidence.
