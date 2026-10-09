# Astra Kubernetes conformance

`scripts/check_astra_kubernetes_conformance.py` is an explicit opt-in, bounded
fixture. It creates only a newly named probe namespace and records private JSON
evidence. It does not attach a target, dispatch work, call a provider, or enable a
Kubernetes execution profile.

NetworkPolicy resources in its result are reported as a **declaration**. The
separate `enforcement` result is emitted only after a three-second allowed client
probe reaches the labelled server and a distinct denied client probe times out.
Missing policies, a permissive denied path, malformed probe output, or an
unattributed Pod UID fail the fixture rather than claiming enforcement. That
evidence is specific to its observed cluster/namespace and is not a Docker Desktop
or general Kubernetes isolation guarantee.
