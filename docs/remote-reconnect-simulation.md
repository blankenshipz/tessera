# Remote reconnect simulation

Run the opt-in local fixture with:

```sh
.venv/bin/python scripts/check_remote_reconnect_synthetic_e2e.py
```

It starts a temporary loopback control service and a separate Python worker
process. It covers durable package/run identity, heartbeat, checkpoint,
unknown-after-control-outage reconnect, and a requested cancellation fence.
The printed `success_failure` value is deliberately `provider-unverified`:
this is a simulation, not execution, provider evidence, or laptop independence.

The desktop reconnect client is covered without Electron by
`npm test -- --test-name-pattern='desktop reconnect'`; closing Electron only
disconnects that client. Neither fixture starts Electron, provisions cloud or
Kubernetes resources, enables failover, makes a held restore writable, nor
claims an external job stopped. Reconcile an unknown operation through the
existing human reconciliation path before issuing replacement work.
