"""Real local bytes/journals over explicitly synthetic schema56/58 databases.

Not genuine application backups, offhost custody, host fencing or cluster evidence.
The separate retained-application tests own genuine backup/held-restore coverage.
"""

import hashlib
import json
import shutil
import sqlite3

from tessera_operator.bundle_export import BundleExport, read_chunk
from tessera_operator.schema_supervised import read_db


def database(path, schema, incarnation):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE schema_meta(schema_version INTEGER);"
            "CREATE TABLE control_recovery(incarnation TEXT);"
        )
        conn.execute("INSERT INTO schema_meta VALUES(?)", (schema,))
        conn.execute("INSERT INTO control_recovery VALUES(?)", (incarnation,))
    return read_db(path.parent, path.name)


def export_phase(root, plan, phase, db_path, input_sha):
    root.mkdir(mode=0o700, parents=True)
    prefix = "pre" if phase == "backup" else "post"
    scratch = root / "scratch"
    bundle = scratch / prefix
    bundle.mkdir(mode=0o700, parents=True)
    shutil.copyfile(db_path, bundle / "snapshot.sqlite3")
    db = read_db(db_path.parent, db_path.name)
    held = root / "held.sqlite3"
    shutil.copyfile(db_path, held)
    # Synthetic held identity, not a call to application restore_state.
    with sqlite3.connect(held) as conn:
        conn.execute("UPDATE control_recovery SET incarnation=?", ("synthetic-held-" + phase,))
    manifest = dict(
        format_version=1,
        backup_id="synthetic-" + phase,
        schema_version=db["schema"],
        sha256=db["sha256"],
        size=db["size"],
    )

    def write(path, value):
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    manifest_sha = write(bundle / "manifest.json", manifest)
    proof = dict(
        operation=plan["operation"],
        phase=phase,
        target=plan["target"],
        source_incarnation=db["incarnation"],
        input_db_sha256=input_sha,
        post_db_sha256=db["sha256"],
        manifest_sha256=manifest_sha,
        snapshot_sha256=db["sha256"],
        state="held_restore",
        held_db=read_db(root, held.name),
    )
    proof_sha = write(scratch / (phase + ".restore-proof.json"), proof)
    remote = dict(
        operation=plan["operation"],
        phase=phase,
        target=plan["target"],
        input_db_sha256=input_sha,
        db=db,
        custody=dict(
            manifest=manifest,
            manifest_sha256=manifest_sha,
            restore_receipt=proof,
            restore_receipt_sha256=proof_sha,
        ),
    )

    class Source:
        identity = dict(
            operation=plan["operation"], phase=phase, root=str(scratch), transport="scripted-local"
        )

        def read(self, name, offset, count):
            return read_chunk(scratch, name, offset, count)

    destination = root / "destination"
    destination.mkdir(mode=0o700)
    journal = root / "export-journal"
    BundleExport(journal, destination, clock=lambda: 100).export(
        plan, phase, {"remote": remote}, Source()
    )
    return remote, dict(journal=str(journal), destination=str(destination))
