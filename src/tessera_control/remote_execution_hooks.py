"""Per-target hooks for the generic remote-route core.

``remote_execution`` looks the hooks up by the selection's target and never branches on
a vendor. A hook runs in the caller's transaction (it receives ``db``/``live``-style
arguments and opens no connection of its own), raises ``Conflict``/``Forbidden`` to
refuse, and must keep every refusal message and tenant check of the code it replaced.
The default hooks add no refusal and no projection.
"""

from .execution_targets import get_target
from .store import Conflict


class RouteHooks:
    """Default hooks: workload_v1 admission only; no extra refusal or projection."""

    # True when the target supports the group-cleanup audit and route snapshot reads.
    group_cleanup = False

    def refuse(self, proposal):
        """Refuse a proposal whose target is not executable. Default: accept."""

    def admit_workload(self, db, project_id, selection, worker_principal_id, *, create_shape):
        """Workload admission evidence (may raise ValueError); None without staged input."""
        from .salad_workload_admission import check_workload_admission

        return check_workload_admission(
            db, project_id, selection, worker_principal_id, create_shape=create_shape
        )

    def on_propose(self, db, op, selection, admit_workload):
        """Propose-time admission and uniqueness guards, inside the proposal transaction.

        ``admit_workload(db, project_id, selection, worker_principal_id)`` raises Conflict.
        """

    def cleanup_group(self, selection):
        """Provider group name recorded on a cleanup audit, or None."""
        return None

    def open_readonly(self, database_path):
        raise NotImplementedError

    def read_route_snapshots(self, database_path, *, org_id, admit):
        raise NotImplementedError

    def parse_cleanup_audit(self, audit):
        """Validated cleanup audit dict; raises ValueError for an unsupported shape."""
        raise NotImplementedError

    def claim_path(self, ident):
        """Idempotency path of the proxy claim; raises when the target has none."""
        raise Conflict("Target has no proxy claim.")

    def claim_proxy(self, store, db, live, ident, effect_id, policy, preflight, recheck):
        """Use-time admission and one-use nonce inside the route transaction.

        Returns ``(status, state)``; ``store`` supplies the route helpers.
        """
        raise Conflict("Target has no proxy claim.")

    def project_state(self, state):
        """Add target-specific read-only fields to a state returned by ``get``."""


_DEFAULT = RouteHooks()


def hooks_for(target):
    factory = get_target(target).route_hooks
    return _DEFAULT if factory is None else factory()


def cleanup_hooks():
    """Hooks of the first registered target that supports group cleanup."""
    from .execution_targets import target_names

    for name in target_names():
        hooks = hooks_for(name)
        if hooks.group_cleanup:
            return hooks
    raise NotImplementedError("No registered target supports group cleanup.")
