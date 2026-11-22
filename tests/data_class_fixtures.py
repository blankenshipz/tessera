"""Synthetic data-class fixtures (ADR 0039). Never use on application state."""

import sqlite3

CLASS_ROW_GUARDS = ("snapshot_data_classes_no_delete", "snapshot_data_classes_no_replace")


def rewrite_class_row(db, snapshot_id):
    """Synthetic fixture only: lift the immutability guards to remove one class row.

    Production has no such path; the guards are restored by ``restore_class_guards``.
    """
    for name in CLASS_ROW_GUARDS:
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    db.execute("DELETE FROM snapshot_data_classes WHERE snapshot_id = ?", (snapshot_id,))


def restore_class_guards(db):
    from tessera_control.data_class import create_data_class_schema

    create_data_class_schema(db)


def declare_fixture_input_public(state, operation_id):
    """Synthetic fixture only: record the route grant's input as a human-declared public
    snapshot (ADR 0039), as a labelled owner staging with evidence would. Salad tickets
    are minted only for public inputs."""
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        snapshot_id, actor = db.execute(
            """SELECT g.snapshot_id, s.actor_principal_id FROM external_operations o
            JOIN execution_grants g ON g.id = o.grant_id
            JOIN source_snapshots s ON s.id = g.snapshot_id WHERE o.operation_id = ?""",
            (operation_id,),
        ).fetchone()
        rewrite_class_row(db, snapshot_id)
        db.execute(
            """INSERT INTO snapshot_data_classes (snapshot_id, data_class, origin, license,
            source, actor_principal_id, decision_class, recorded_at)
            VALUES (?, 'public', 'human_declared', 'CC0-1.0',
            'https://example.org/synthetic-fixture', ?, 'owner_automation_labelled',
            '2026-01-01T00:00:00Z')""",
            (snapshot_id, actor),
        )
        restore_class_guards(db)
    return snapshot_id
