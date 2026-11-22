# Execution targets

Compute vendors plug in through one neutral seam. No vendor-specific branch
belongs in `tessera_control/remote_execution.py` or
`tessera_worker/remote_dispatch.py`; both are generic over registered targets.

**Current gap.** This is the rule, not yet the whole code: `remote_execution.py` still
contains a Salad inference branch and Salad SQL for the non-campaign route, and
`tessera_worker/adapter_registry.py` is exercised only by tests. Campaign execution does not
use that branch; it goes through the `ComputeProvider` seam. New vendors must not add to it.

## Adding a vendor

1. **Target spec.** In `tessera_control/execution_targets.py` call
   `register_target(TargetSpec(name=..., admission_hook=...))`. The `Target`
   type used by `ExecutionSelection` accepts only registered names, so an
   unregistered name is a validation error, never a server error. Names are
   unique; duplicate registration raises.
2. **Admission hook (optional).** `admission_hook(db, live, proposal, operation)
   -> dict | None` runs inside the live-authority transaction on create/start
   (route core and `TrustedSaladServer.preflight_authority`). It must raise
   `Conflict`/`Forbidden` to refuse; a returned dict is stored as
   `state["budget_binding"]`. Keep vendor imports lazy inside the hook; core
   modules such as `experiment_budgets` must not import vendor code.
3. **Adapter.** Implement `RemoteAdapter` (`preflight(proposal)`,
   `perform(action, proposal, state)`) for the route dispatcher, and/or the
   `ExecutionAdapter` protocol in `tessera_worker/execution_contracts.py`.
   If preflight needs the action, define `preflight_for(action, proposal)`; the
   dispatcher prefers it. Server-composed providers that mint provider evidence
   must declare `trusted_composition = True` and expose `trusted_store`; the
   dispatcher only grants trusted-server mode when that store is its own.
4. **Registration.** `tessera_worker/adapter_registry.py`:
   `register_adapter(adapter_kind, factory)`; look up with `adapter_factory(kind)`
   and list with `adapter_kinds()`. `adapter_kind` matches
   `TargetBinding.adapter_kind`. Registration grants no authority.

Existing targets: `kubernetes`, `salad_container` (budget-binding hook),
`salad_inference`.
