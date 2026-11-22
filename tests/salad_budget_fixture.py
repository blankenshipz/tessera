"""Synthetic experiment-budget parent for Salad route fixtures.

Salad create/start require the reservation written at native admission, so the
parent is authorized BEFORE the admitted attempt exists; no post-hoc reservation
is fabricated and no production authority is issued.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest import mock


@contextmanager
def budgeted_admission():
    from tessera_control.experiment_budgets import authorize_budget
    from tessera_control.experiments import ExperimentStore

    original = ExperimentStore._admit_attempt_transaction

    def admit(store, db, actor, variant_id, body):
        experiment_id = store._variant(db, actor, variant_id)["experiment_id"]
        if not db.execute(
            "SELECT 1 FROM experiment_budgets WHERE experiment_id=?", (experiment_id,)
        ).fetchone():
            authorize_budget(
                db,
                actor,
                experiment_id,
                dict(
                    expected_revision=0,
                    ceiling_cents=5000,
                    expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    evidence_digest="f" * 64,
                    source_reference="synthetic-parent",
                    prior_settled_cents=0,
                    prior_pending_cents=0,
                    ancillary_cents=500,
                    shutdown_cents=1000,
                ),
            )
        return original(store, db, actor, variant_id, body)

    with mock.patch.object(ExperimentStore, "_admit_attempt_transaction", admit):
        yield
