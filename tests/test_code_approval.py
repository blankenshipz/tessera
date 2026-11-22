"""Owner-approved private code in a staged input (ADR 0039, amendment 1).

Approved code or config may reach third-party compute next to data declared public;
every other file must still be public data. Synthetic state only; no provider,
network or credential is contacted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from data_class_fixtures import restore_class_guards, rewrite_class_row
from test_artifact_custody import _released_manifest
from test_chunked_snapshots import _bytes_of, _chunk_body, _inventory
from test_dataset_staging import _body as _staging_body
from test_salad_input_path import PUBLIC_INPUT, input_contract, setup_contract, staged_dir
from test_salad_live_check import LocalSession, drive
from test_salad_live_setup import inputs, open_session, private_file, setup_script  # noqa: F401

from tessera_control.code_approval import (
    MAX_CODE_FILE_BYTES,
    CodeApproval,
    _require_approver,
    admitted_code_paths,
    create_code_approval_schema,
    input_classes,
    is_code_path,
)
from tessera_control.data_class import require_public_input
from tessera_control.dataset_staging import DatasetStagingStore
from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.snapshot_chunks import ChunkedSnapshotStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, Forbidden, StoreError, _connect
from tessera_control.workspace import capture_workspace, inventory_digest

CODE_CLASSES = ("public", "private_code_approved")
PUBLIC = {"data_class": "public", "license": "CC0-1.0", "source": "https://example.org/data"}
CODE = b"print('approved')\n"
FILES = {"main.py": CODE, "conf/run.toml": b"steps = 3\n", "data/rows.csv": b"x\n1\n2\n"}
APPROVED = {
    "paths": ["main.py", "conf/run.toml"],
    "structured_config_confirmed": ["conf/run.toml"],
}


def _labelled(owner):
    return dict(owner, automation_label="code-approval-test")


def _bare(owner):
    return {k: v for k, v in owner.items() if k != "automation_label"}


def _begin(store, actor, project_id, files, key, *, data_class=None, code=None, inventory=None):
    manifest, digest = inventory or _inventory(files)
    body = {"expected_digest": digest, "inventory": manifest}
    if data_class is not None:
        body["data_class"] = data_class
    if code is not None:
        body["code_approval"] = code
    return store.begin_upload(actor, project_id, key, body)


def _chunked(store, actor, project_id, files, key, *, data_class=None, code=None, complete=None):
    status, begun = _begin(
        store, actor, project_id, files, key + "-begin", data_class=data_class, code=code
    )
    assert status == 201
    for planned in begun["chunk_plan"]:
        store.put_chunk(
            actor,
            project_id,
            begun["upload_id"],
            planned["seq"],
            _chunk_body(_bytes_of(files, planned)),
        )
    return store.complete_upload(actor, project_id, begun["upload_id"], key + "-done", complete)


def _selection_for(digest):
    from test_salad_http import selection
    from test_salad_workload_contract import contract as base_contract
    from test_salad_workload_contract import execution

    value = selection().model_dump(mode="json")
    value["manifests"]["data"] = digest
    work = base_contract(
        input=dict(kind="staged_snapshot", snapshot_digest=digest, max_bytes=1 << 20)
    )
    # Input that is not recorded public (approved code) needs the trust-policy route literal.
    value["salad"]["execution"] = execution(work, data_policy="provider_trust_policy_v1")
    return value


def _entry_selection(digest, entry_path):
    """A gpu_runtime_v1 selection whose inline manifest runs an input entry."""
    from test_gpu_runtime_entry_input import ENTRY, contract_value, manifest_of
    from test_salad_http import selection
    from test_salad_workload_contract import execution

    value = selection().model_dump(mode="json")
    value["manifests"]["data"] = digest
    work = contract_value(manifest_of(dict(ENTRY, path=entry_path)), digest=digest)
    work["input"]["max_bytes"] = 1 << 20
    work["argv_template"][9] = f"input:{digest}:{1 << 20}"
    value["salad"]["execution"] = execution(work, data_policy="provider_trust_policy_v1")
    return value


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- model


def test_only_code_and_config_paths_can_be_approved():
    for path in ("a.py", "x/run.sh", "c.json", "p.toml", "k.yaml", "k.yml", "s.cfg", "s.ini"):
        assert is_code_path(path), path
    for path in ("requirements.txt", "env/requirements-gpu.txt", "constraints.txt", "A.PY"):
        assert is_code_path(path), path
    assert is_code_path("constraints-cu12.txt")
    for path in (
        "requirements-a-b.txt",
        "requirements_dev.txt",
        "requirements.data.txt",
        "requirements-rows.v2.txt",
        "requirementsfoo.txt",
        "my-requirements.txt",
    ):
        assert not is_code_path(path), path
    for path in ("notes.txt", "rows.csv", "rows.jsonl", "w.bin", "nb.ipynb", "Makefile", ".py"):
        assert not is_code_path(path), path
    assert CodeApproval.model_validate({"paths": ["b.py", "a.py"]}).paths == ["a.py", "b.py"]
    ok = {"paths": ["c.json", "a.py"], "structured_config_confirmed": ["c.json"]}
    assert CodeApproval.model_validate(ok).structured_config_confirmed == ["c.json"]
    for bad in (
        {"paths": []},
        {"paths": ["a.py", "a.py"]},
        {"paths": ["../a.py"]},
        {"paths": ["/abs.py"]},
        {"paths": ["data.txt"]},
        {"paths": ["a.py"], "public": True},
        {"paths": ["c.json"]},  # structured and not confirmed
        {"paths": ["a.py"], "structured_config_confirmed": ["a.py"]},  # not structured
        {"paths": ["a.py"], "structured_config_confirmed": ["c.json"]},  # not approved
        {"paths": ["c.json"], "structured_config_confirmed": ["c.json", "c.json"]},
    ):
        with pytest.raises(ValueError):
            CodeApproval.model_validate(bad)


# ------------------------------------------------------------------------- staging


def test_approved_code_plus_public_data_is_recorded_and_admitted(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    status, snap = _chunked(
        store, _labelled(owner), project_id, FILES, "mixed", data_class=PUBLIC, code=APPROVED
    )
    assert status == 201
    code = snap["code_approval"]
    assert code["code"] == "private_code_approved" and code["valid"] is True
    assert code["files"] == [
        {
            "relative_path": "conf/run.toml",
            "sha256": _sha(FILES["conf/run.toml"]),
            "size": 10,
            "structured_config_confirmed": True,
        },
        {"relative_path": "main.py", "sha256": _sha(CODE), "size": len(CODE)},
    ]
    assert code["decision_class"] == "owner_automation_labelled"
    assert code["actor_principal_id"] == owner["id"]
    # The data record is unchanged: public covers the data files; the code stays private.
    assert snap["data_class"]["data_class"] == "public"
    assert store.get_snapshot(owner, snap["id"])["code_approval"] == code
    with closing(_connect(state / "tessera.sqlite3")) as db:
        assert admitted_code_paths(db, snap["id"]) == {"main.py", "conf/run.toml"}
        # ADR 0040: approved code needs private_code_approved for the provider instance,
        # even though the snapshot's data is recorded public. The default refuses it.
        with pytest.raises(ValueError, match="private_code_approved"):
            require_public_input(db, "salad_container", snap["id"], _w["id"])
        with pytest.raises(ValueError, match="private_code_approved"):
            check_workload_admission(db, project_id, _selection_for(snap["digest"]), _w["id"])
        trust_policy({"salad": (CODE_CLASSES, [_w["id"]])})
        with pytest.raises(ValueError, match="private_code_approved"):  # unbound principal
            require_public_input(db, "salad_container", snap["id"], "someone-else")
        assert require_public_input(db, "salad_container", snap["id"], _w["id"]) == {
            "main.py",
            "conf/run.toml",
        }
        check_workload_admission(db, project_id, _selection_for(snap["digest"]), _w["id"])
        assert input_classes(db, snap["id"]) == {
            "input_data_class": "public",
            "input_code": "private_code_approved",
            "input_code_files": [
                {"relative_path": "conf/run.toml", "sha256": _sha(FILES["conf/run.toml"])},
                {"relative_path": "main.py", "sha256": _sha(CODE)},
            ],
        }
        (event,) = db.execute(
            "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted' "
            "AND json_extract(payload_json,'$.snapshot_id')=?",
            (snap["id"],),
        ).fetchall()
    assert json.loads(event[0])["code_approval"]["files"] == code["files"]


def test_code_only_input_is_admitted_and_reports_no_data_class(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    files = {"main.py": CODE}
    _, snap = _chunked(
        ChunkedSnapshotStore(state),
        _labelled(owner),
        project_id,
        files,
        "code-only",
        code={"paths": ["main.py"]},
    )
    assert snap["data_class"]["data_class"] == "private_derived"  # never relabelled
    trust_policy({"salad": (CODE_CLASSES, [_w["id"]])})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        check_workload_admission(db, project_id, _selection_for(snap["digest"]), _w["id"])
        classes = input_classes(db, snap["id"])
    assert classes["input_data_class"] == "none_all_approved_code"
    assert classes["input_code"] == "private_code_approved"


def test_unapproved_code_or_private_data_next_to_approved_code_is_refused(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    # Approved code, but the other files are not declared public.
    _, private = _chunked(store, actor, project_id, FILES, "private", code=APPROVED)
    # Unapproved code: helper.py is neither approved nor covered by a public record.
    extra = dict(FILES, **{"helper.py": b"x = 1\n"})
    _, unapproved = _chunked(store, actor, project_id, extra, "unapproved", code=APPROVED)
    # No approval and no public declaration (the original rule).
    _, plain = _chunked(store, actor, project_id, {"main.py": b"y = 2\n"}, "plain")
    # The provider may receive approved code, but not private data (also the default's
    # refusal: no principal is bound, so every instance gets public only).
    trust_policy({"salad": (CODE_CLASSES, [_w["id"]])})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        for snap in (private, unapproved, plain):
            with pytest.raises(ValueError, match="public"):
                check_workload_admission(db, project_id, _selection_for(snap["digest"]), _w["id"])
            with pytest.raises(ValueError, match="public"):
                require_public_input(db, "salad_container", snap["id"], _w["id"])
            # ADR 0040: no target is exempt (the worker is bound to salad, not kubernetes).
            with pytest.raises(ValueError, match="public"):
                require_public_input(db, "kubernetes", snap["id"], _w["id"])
        assert input_classes(db, private["id"])["input_data_class"] == "private_derived"


def test_non_code_extensions_unknown_paths_and_oversize_files_are_refused(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    for code in ({"paths": ["data/rows.csv"]}, {"paths": ["absent.py"]}):
        with pytest.raises(StoreError):
            _begin(store, actor, project_id, FILES, "bad-" + json.dumps(code), code=code)
    big = {"big.py": b"#" * (MAX_CODE_FILE_BYTES + 1)}
    with pytest.raises(StoreError, match="exceeds"):
        _begin(store, actor, project_id, big, "big", code={"paths": ["big.py"]})
    many = {f"m{i}.py": bytes([i]) * MAX_CODE_FILE_BYTES for i in range(9)}
    with pytest.raises(StoreError, match="in total"):
        _begin(store, actor, project_id, many, "many", code={"paths": sorted(many)})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM snapshot_code_approvals").fetchone()[0] == 0


def test_bytes_held_as_derived_or_migrated_data_cannot_be_approved_as_code(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    _, derived = DatasetStagingStore(state).stage(
        actor, project_id, "derived", _staging_body(state, manifest)
    )
    leaked = derived["staging"]["files"][0]
    # Same bytes as a run output, renamed to look like code. Refused at begin, before
    # any chunk is uploaded.
    entry = {
        "relative_path": "copy.py",
        "size": leaked["size"],
        "sha256": leaked["sha256"],
        "provenance": "imported_unverified",
        "classification": "unclassified",
    }
    inventory = {"root": "synthetic", "entries": [entry], "exclusions": [], "total_bytes": 0}
    inventory["total_bytes"] = leaked["size"]
    digest = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _begin(
            store,
            actor,
            project_id,
            None,
            "launder",
            code={"paths": ["copy.py"]},
            inventory=(inventory, digest),
        )
    # Migrated bytes are refused the same way, at begin and again at completion.
    old = {"old.py": b"legacy = True\n"}
    _, migrated = _chunked(store, actor, project_id, old, "old")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rewrite_class_row(db, migrated["id"])
        db.execute(
            "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,recorded_at) "
            "VALUES (?,'private_derived','migrated','2026-01-01T00:00:00Z')",
            (migrated["id"],),
        )
        restore_class_guards(db)
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _chunked(store, actor, project_id, old, "launder-old", code={"paths": ["old.py"]})
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _chunked(store, actor, project_id, old, "late", complete={"code_approval": {
            "paths": ["old.py"]}})  # fmt: skip
    # Bytes staged earlier without any declaration (origin default) are private data too.
    mine = {"mine.py": b"mine = 1\n"}
    _chunked(store, actor, project_id, mine, "first")
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _chunked(store, actor, project_id, mine, "again", code={"paths": ["mine.py"]})
    # The same code may be staged again once it is validly approved elsewhere.
    ok = {"ok.py": b"ok = 1\n"}
    _chunked(store, actor, project_id, ok, "ok1", code={"paths": ["ok.py"]})
    _, again = _chunked(store, actor, project_id, ok, "ok2", code={"paths": ["ok.py"]})
    assert again["code_approval"]["valid"] is True
    # An empty file carries no content and is exempt.
    _chunked(store, actor, project_id, {"e.py": b""}, "e1")
    _chunked(store, actor, project_id, {"e.py": b""}, "e2", code={"paths": ["e.py"]})


def test_public_declaration_refuses_a_derived_file_bundled_with_new_approved_code(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    _, derived = DatasetStagingStore(state).stage(
        actor, project_id, "derived-pf", _staging_body(state, manifest)
    )
    leaked_sha = derived["staging"]["files"][0]["sha256"]
    with closing(_connect(state / "tessera.sqlite3")) as db:
        (leaked,) = db.execute(
            "SELECT content FROM snapshot_files WHERE snapshot_id=? LIMIT 1",
            (derived["id"],),
        ).fetchone()
    assert _sha(leaked) == leaked_sha
    mixed = {"main.py": b"fresh = 1\n", "rows.dat": leaked}
    bundle = {"paths": ["main.py"]}
    # Chunked begin, chunked complete and local adoption all refuse it.
    with pytest.raises(Conflict, match="cannot be declared public"):
        _begin(store, actor, project_id, mixed, "pf-begin", data_class=PUBLIC, code=bundle)
    status, begun = _begin(store, actor, project_id, mixed, "pf-late", code=bundle)
    assert status == 201
    for planned in begun["chunk_plan"]:
        store.put_chunk(
            actor, project_id, begun["upload_id"], planned["seq"],
            _chunk_body(_bytes_of(mixed, planned)),
        )  # fmt: skip
    with pytest.raises(Conflict, match="cannot be declared public"):
        store.complete_upload(
            actor, project_id, begun["upload_id"], "pf-done", {"data_class": PUBLIC}
        )
    root = tmp_path / "pf-local"
    root.mkdir()
    (root / "main.py").write_bytes(b"fresh = 1\n")
    (root / "rows.dat").write_bytes(leaked)
    capture = capture_workspace(str(root))
    with pytest.raises(Conflict, match="cannot be declared public"):
        SnapshotStore(state).adopt_snapshot(
            actor, project_id, "pf-adopt", inventory_digest(capture.inventory), capture,
            data_class=PUBLIC, code_approval=bundle,
        )  # fmt: skip
    # Genuinely new data next to approved code, and an empty file, are fine.
    ok = {"main.py": b"fresh = 1\n", "rows.dat": b"new rows\n", "empty.dat": b""}
    _, snap = _chunked(store, actor, project_id, ok, "pf-ok", data_class=PUBLIC, code=bundle)
    assert snap["data_class"]["data_class"] == "public"


def test_setup_summary_shows_confirmed_structured_files():
    args = SimpleNamespace(
        input_data_class=None,
        input_code_approval=[
            {"relative_path": "run.py", "sha256": "a" * 64, "size": 1},
            {
                "relative_path": "conf/run.json",
                "sha256": "b" * 64,
                "size": 1,
                "structured_config_confirmed": True,
            },
        ],
    )
    basis = setup_script._input_basis(args)
    assert "confirmed by the owner as configuration, not data" in basis
    assert "conf/run.json" in basis
    del args.input_code_approval[1]["structured_config_confirmed"]
    assert "structured" not in setup_script._input_basis(args)


def test_structured_config_needs_confirmation_and_stays_small(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    conf = {"run_config.json": b'{"steps": 3}\n'}
    # Unconfirmed: refused (model level).
    with pytest.raises(StoreError):
        _begin(store, actor, project_id, conf, "u", code={"paths": ["run_config.json"]})
    confirmed = {"paths": ["run_config.json"], "structured_config_confirmed": ["run_config.json"]}
    # Confirmed and small: accepted, and the confirmation is in the immutable row.
    _, snap = _chunked(store, actor, project_id, conf, "c", code=confirmed)
    (file,) = snap["code_approval"]["files"]
    assert file["structured_config_confirmed"] is True and snap["code_approval"]["valid"]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        (stored,) = db.execute(
            "SELECT files_json FROM snapshot_code_approvals WHERE snapshot_id=?", (snap["id"],)
        ).fetchone()
    assert json.loads(stored)[0]["structured_config_confirmed"] is True
    # Confirmed but over 64 KiB: refused.
    big = {"big.json": b"{" + b" " * (64 * 1024) + b"}"}
    with pytest.raises(StoreError, match="exceeds"):
        _begin(
            store, actor, project_id, big, "b",
            code={"paths": ["big.json"], "structured_config_confirmed": ["big.json"]},
        )  # fmt: skip
    # A stored row that drops the confirmation no longer matches (fails closed).
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("DROP TRIGGER snapshot_code_approvals_no_update")
        db.execute("UPDATE snapshot_code_approvals SET files_json=replace(files_json, "
                   "',\"structured_config_confirmed\":true', '')")  # fmt: skip
    with closing(_connect(state / "tessera.sqlite3")) as db:
        assert input_classes(db, snap["id"])["input_code"] == "invalid"


def test_confirmed_structured_file_of_derived_bytes_is_still_refused(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    _, derived = DatasetStagingStore(state).stage(
        actor, project_id, "derived-json", _staging_body(state, manifest)
    )
    leaked = derived["staging"]["files"][0]
    entry = {
        "relative_path": "copy.json",
        "size": leaked["size"],
        "sha256": leaked["sha256"],
        "provenance": "imported_unverified",
        "classification": "unclassified",
    }
    inventory = {"root": "synthetic", "entries": [entry], "exclusions": [], "total_bytes": 0}
    inventory["total_bytes"] = leaked["size"]
    digest = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert leaked["size"] <= 64 * 1024
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _begin(
            ChunkedSnapshotStore(state), actor, project_id, None, "launder-json",
            code={"paths": ["copy.json"], "structured_config_confirmed": ["copy.json"]},
            inventory=(inventory, digest),
        )  # fmt: skip


def _allow_any_origin(db):
    """Synthetic fixture only: a class table without the origin CHECK, to model an
    origin that does not exist yet."""
    db.execute("ALTER TABLE snapshot_data_classes RENAME TO _old_classes")
    db.execute(
        "CREATE TABLE snapshot_data_classes (snapshot_id TEXT PRIMARY KEY, data_class TEXT, "
        "origin TEXT, license TEXT, source TEXT, actor_principal_id TEXT, decision_class TEXT, "
        "provenance_json TEXT, recorded_at TEXT NOT NULL)"
    )
    db.execute("INSERT INTO snapshot_data_classes SELECT * FROM _old_classes")
    db.execute("DROP TABLE _old_classes")


def test_any_non_public_record_blocks_laundering_including_future_origins(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    held = {"held.py": b"held = 1\n"}
    _, first = _chunked(store, actor, project_id, held, "held")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for name in ("snapshot_data_classes_no_update", "snapshot_data_classes_no_delete",
                     "snapshot_data_classes_no_replace"):  # fmt: skip
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        _allow_any_origin(db)
        db.execute(
            "UPDATE snapshot_data_classes SET origin='private_future' WHERE snapshot_id=?",
            (first["id"],),
        )
    with pytest.raises(Conflict, match="cannot be approved as code"):
        _chunked(store, actor, project_id, held, "again", code={"paths": ["held.py"]})
    # The per-file public check mirrors it: a public snapshot cannot carry those bytes.
    mixed = {"new.py": b"new = 1\n", "rows.dat": held["held.py"]}
    with pytest.raises(Conflict, match="cannot be declared public"):
        _begin(store, actor, project_id, mixed, "pub", data_class=PUBLIC,
               code={"paths": ["new.py"]})  # fmt: skip
    # Bytes already validly approved as code elsewhere may still be staged again.
    ok = {"ok.py": b"ok = 1\n"}
    _chunked(store, actor, project_id, ok, "ok1", code={"paths": ["ok.py"]})
    _chunked(store, actor, project_id, ok, "ok2", code={"paths": ["ok.py"]})


def test_public_snapshot_cannot_carry_another_public_snapshots_approved_code(tmp_path):
    """A public snapshot's approved code is private code, not public data: its bytes may
    not reappear unapproved as "public data" in another public snapshot."""
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    _chunked(store, actor, project_id, FILES, "a", data_class=PUBLIC, code=APPROVED)
    smuggled = {"copy.dat": CODE, "rows2.csv": b"y\n3\n"}
    with pytest.raises(Conflict, match="approved as private"):
        _begin(store, actor, project_id, smuggled, "b", data_class=PUBLIC)
    # Staging the identical snapshot again is governed by the whole-snapshot rule
    # (ADR 0040 ranks identical stagings), not by this per-file rule.
    _chunked(store, actor, project_id, FILES, "same", data_class=PUBLIC)
    # Approving the same bytes as code in the new snapshot is the legitimate path.
    reused = {"copy.py": CODE, "rows2.csv": b"y\n3\n"}
    _chunked(store, actor, project_id, reused, "c", data_class=PUBLIC,
             code={"paths": ["copy.py"]})  # fmt: skip


def test_a_corrupt_manifest_elsewhere_does_not_block_public_staging(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    _, other = _chunked(store, actor, project_id, {"held.csv": b"h\n"}, "held")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        triggers = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='source_snapshots'"
        )]  # fmt: skip
        for name in triggers:
            db.execute(f"DROP TRIGGER {name}")
        db.execute("UPDATE source_snapshots SET manifest_json='{not json' WHERE id=?",
                   (other["id"],))  # fmt: skip
    _chunked(store, actor, project_id, {"fresh.csv": b"f\n"}, "fresh", data_class=PUBLIC)


def test_this_snapshots_malformed_manifest_fails_closed_for_it_only(tmp_path):
    from tessera_control.data_class import refuse_relabelled_files

    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    _, snap = _chunked(store, _labelled(owner), project_id, {"p.csv": b"p\n"}, "p",
                       data_class=PUBLIC)  # fmt: skip
    with closing(_connect(state / "tessera.sqlite3")) as db:
        org = db.execute(
            "SELECT p.org_id FROM projects p JOIN source_snapshots s ON s.project_id=p.id "
            "WHERE s.id=?",
            (snap["id"],),
        ).fetchone()[0]
        for bad in (
            [{"relative_path": "p.csv"}],
            [{"relative_path": "p.csv", "size": "x", "sha256": "0" * 64}],
            ["entry"],
        ):
            with pytest.raises(Conflict, match="malformed"):
                refuse_relabelled_files(db, org, snap["id"], entries=bad, digest="0" * 64)


def test_approved_elsewhere_only_reads_approvals_that_list_the_digests(tmp_path):
    from tessera_control.code_approval import _approved_elsewhere

    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    _chunked(store, _labelled(owner), project_id, {"e.py": b"e = 1\n"}, "e",
             code={"paths": ["e.py"]})  # fmt: skip
    with closing(_connect(state / "tessera.sqlite3")) as db:
        org = db.execute("SELECT org_id FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        wanted = {_sha(b"e = 1\n"), "f" * 64}
        assert _approved_elsewhere(db, org, "snapshot_none", wanted) == {_sha(b"e = 1\n")}
        assert _approved_elsewhere(db, org, "snapshot_none", {"f" * 64}) == set()
        assert _approved_elsewhere(db, org, "snapshot_none", set()) == set()


def test_a_release_of_a_snapshot_with_approved_code_is_labelled(tmp_path):
    from test_snapshot_releases import _release, _release_body

    from tessera_control.snapshot_releases import SnapshotReleaseStore

    state, owner, worker, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    _, coded = _chunked(store, actor, project_id, FILES, "coded", data_class=PUBLIC,
                        code=APPROVED)  # fmt: skip
    _, plain = _chunked(store, actor, project_id, {"p.csv": b"p\n"}, "plain",
                        data_class=PUBLIC)  # fmt: skip
    releases = SnapshotReleaseStore(state)
    status, labelled = _release(releases, actor, coded, _release_body(coded, worker), "r1")
    assert status == 201 and labelled["contains_approved_code"] is True
    assert labelled["classification"] == "public"
    status, unlabelled = _release(releases, actor, plain, _release_body(plain, worker), "r2")
    assert status == 201 and unlabelled["contains_approved_code"] is False
    with closing(_connect(state / "tessera.sqlite3")) as db:
        payloads = [
            json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_released'"
            )
        ]  # fmt: skip
    flags = {p["snapshot_id"]: p["contains_approved_code"] for p in payloads}
    assert flags[coded["id"]] is True and flags[plain["id"]] is False


def test_unverified_desktop_owner_automation_cannot_approve_code(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    desktop = dict(owner, automation_label="desktop-owner")
    with pytest.raises(Forbidden):
        _begin(store, desktop, project_id, {"d.py": b"d = 1\n"}, "desk", code={"paths": ["d.py"]})
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
                actor_principal_id, decision_class, provenance_json, recorded_at)
                VALUES ('x', '[]', ?, 'owner_automation_unverified', '{}', 't')""",
                (owner["id"],),
            )


def test_approved_code_is_not_delivered_through_local_worker_transfer(tmp_path):
    from test_input_delivery import _delivery

    from tessera_control.input_delivery import InputDeliveryStore
    from tessera_control.store import canonical_digest

    state, _e, owner, worker, grant, lease = _delivery(tmp_path)
    store = InputDeliveryStore(state)
    body = {"expected_epoch": lease["epoch"]}
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
            actor_principal_id, decision_class, provenance_json, recorded_at)
            VALUES (?, '[]', ?, 'human_session', '{}', 't')""",
            (grant["snapshot_id"], owner["id"]),
        )
    with pytest.raises(Forbidden, match="approved private code"):
        store.deliver_inputs(worker, lease["id"], "inputs", canonical_digest(body), lease["epoch"])


def test_malformed_manifest_is_refused_not_a_keyerror(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    _, snap = _chunked(
        ChunkedSnapshotStore(state), _labelled(owner), project_id, FILES, "mal",
        data_class=PUBLIC, code=APPROVED,
    )  # fmt: skip
    for bad in (
        json.dumps({"entries": [{"size": 1}]}),
        json.dumps({"nope": []}),
        json.dumps({"entries": "x"}),
        "not json",
    ):
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE source_snapshots SET manifest_json=? WHERE id=?", (bad, snap["id"]))
        with closing(_connect(state / "tessera.sqlite3")) as db:
            with pytest.raises(ValueError):  # DataClassRefused, never KeyError
                admitted_code_paths(db, snap["id"])
            assert input_classes(db, snap["id"])["input_code"] == "invalid"


V62_FIXTURE = Path(__file__).parent / "fixtures" / "state_v62.sql"


def _v62_state(tmp_path):
    """A real schema-v62 database built by v62 code (dev/make_v62_state_fixture.py)."""
    state = tmp_path / "v62"
    state.mkdir(mode=0o700)
    database = state / "tessera.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.executescript(V62_FIXTURE.read_text())
    database.chmod(0o600)
    return state, database


def test_v62_migration_leaves_existing_snapshots_unapproved(tmp_path):
    from tessera_control.store import migrate_state

    state, db_path = _v62_state(tmp_path)
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone() == (62,)
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='snapshot_code_approvals'"
            ).fetchone()
            is None
        )
        snapshots = db.execute("SELECT id, digest FROM source_snapshots ORDER BY id").fetchall()
        columns = [r[1] for r in db.execute("PRAGMA table_info(snapshot_data_classes)")]
        listed = ",".join(columns)
        classes = db.execute(
            f"SELECT {listed} FROM snapshot_data_classes ORDER BY snapshot_id"
        ).fetchall()
    assert len(snapshots) >= 2
    assert migrate_state(state) == 72
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT count(*) FROM snapshot_code_approvals").fetchone() == (0,)
        assert db.execute("SELECT id, digest FROM source_snapshots ORDER BY id").fetchall() == (
            snapshots
        )
        assert (
            db.execute(
                f"SELECT {listed} FROM snapshot_data_classes ORDER BY snapshot_id"
            ).fetchall()
            == classes
        )
    with closing(_connect(db_path)) as db:
        for snapshot_id, _ in snapshots:
            assert input_classes(db, snapshot_id)["input_code"] == "none"


def test_only_a_human_decision_actor_may_approve_code(tmp_path):
    from test_snapshots import _agent

    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    with pytest.raises(Forbidden):
        _begin(store, _bare(owner), project_id, FILES, "bare", code=APPROVED)
    agent = _agent(state, owner, project_id)
    with pytest.raises(Forbidden):
        _begin(store, agent, project_id, FILES, "agent", code=APPROVED)
    for actor in (
        {"role": "agent", "kind": "agent", "id": "a"},
        {"role": "human", "kind": "local_owner", "id": "o"},
        {"role": "human", "kind": "worker", "id": "w"},
    ):
        with pytest.raises(Forbidden):
            _require_approver(actor)
    # Local adoption takes the same decision rule.
    root = tmp_path / "local"
    (root / "conf").mkdir(parents=True)
    (root / "main.py").write_bytes(b"z = 3\n")
    capture = capture_workspace(str(root))
    with pytest.raises(Forbidden):
        SnapshotStore(state).adopt_snapshot(
            _bare(owner),
            project_id,
            "adopt-bare",
            inventory_digest(capture.inventory),
            capture,
            code_approval={"paths": ["main.py"]},
        )
    _, adopted = SnapshotStore(state).adopt_snapshot(
        _labelled(owner),
        project_id,
        "adopt",
        inventory_digest(capture.inventory),
        capture,
        code_approval={"paths": ["main.py"]},
    )
    assert adopted["code_approval"]["decision_class"] == "owner_automation_labelled"


def test_completion_may_add_but_never_change_the_begin_approval(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    with pytest.raises(Conflict, match="differs"):
        _chunked(store, actor, project_id, FILES, "change", data_class=PUBLIC, code=APPROVED,
                 complete={"code_approval": {"paths": ["main.py"]}})  # fmt: skip
    _, late = _chunked(store, actor, project_id, {"late.py": b"late\n"}, "late",
                       complete={"code_approval": {"paths": ["late.py"]}})  # fmt: skip
    assert late["code_approval"]["files"][0]["relative_path"] == "late.py"


def test_entry_outside_the_approved_code_is_refused_at_admission(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    files = dict(FILES, **{"other.py": b"other = 1\n"})
    _, snap = _chunked(
        ChunkedSnapshotStore(state),
        _labelled(owner),
        project_id,
        files,
        "entry",
        data_class=PUBLIC,
        code=APPROVED,
    )
    worker = _w["id"]
    trust_policy({"salad": (CODE_CLASSES, [worker])})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        check_workload_admission(
            db, project_id, _entry_selection(snap["digest"], "main.py"), worker
        )
        # other.py is covered by the public data record, but it is not approved code.
        with pytest.raises(ValueError, match="not approved private code"):
            check_workload_admission(
                db, project_id, _entry_selection(snap["digest"], "other.py"), worker
            )
        # With private_derived the instance may receive any data, so the entry rule that
        # protects an approved-code-only admission no longer applies (ADR 0040 section 4).
        trust_policy({"salad": ([*CODE_CLASSES, "private_derived"], [worker])})
        check_workload_admission(
            db, project_id, _entry_selection(snap["digest"], "other.py"), worker
        )


# ------------------------------------------------------------- record integrity


def test_code_approval_is_immutable_and_fails_closed_when_tampered(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    _, snap = _chunked(store, actor, project_id, FILES, "imm", data_class=PUBLIC, code=APPROVED)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE snapshot_code_approvals SET files_json='[]'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM snapshot_code_approvals WHERE snapshot_id=?", (snap["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                """INSERT OR REPLACE INTO snapshot_code_approvals (snapshot_id, files_json,
                actor_principal_id, decision_class, provenance_json, recorded_at)
                VALUES (?, '[]', ?, 'human_session', '{}', 't')""",
                (snap["id"], owner["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
                actor_principal_id, decision_class, provenance_json, recorded_at)
                VALUES ('x', '[]', ?, 'other_credential', '{}', 't')""",
                (owner["id"],),
            )
    # A begin-time approval of an upload that never completes may still be discarded.
    _, begun = _begin(store, actor, project_id, {"never.py": b"n\n"}, "never",
                      code={"paths": ["never.py"]})  # fmt: skip
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("DELETE FROM snapshot_code_approvals WHERE snapshot_id=?", (begun["upload_id"],))
    # A row that no longer matches the snapshot's files fails closed, even with a public
    # data record (synthetic tampering with the guards lifted).
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("DROP TRIGGER snapshot_code_approvals_no_delete")
        db.execute("DROP TRIGGER snapshot_code_approvals_no_replace")
        db.execute("DELETE FROM snapshot_code_approvals WHERE snapshot_id=?", (snap["id"],))
        db.execute(
            """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
            actor_principal_id, decision_class, provenance_json, recorded_at)
            VALUES (?, ?, ?, 'human_session', '{}', 't')""",
            (
                snap["id"],
                json.dumps([{"relative_path": "main.py", "sha256": "0" * 64, "size": 1}]),
                owner["id"],
            ),
        )
        create_code_approval_schema(db)
    with closing(_connect(state / "tessera.sqlite3")) as db:
        with pytest.raises(ValueError, match="does not match"):
            require_public_input(db, "salad_container", snap["id"])
        # An invalid approval is refused under any trust setting (ADR 0040 section 4).
        trust_policy({"salad": ([*CODE_CLASSES, "private_derived"], [_w["id"]])})
        with pytest.raises(ValueError, match="does not match"):
            require_public_input(db, "salad_container", snap["id"], _w["id"])
        # A tampered approval shows as invalid on receipts, never as "none".
        assert input_classes(db, snap["id"])["input_code"] == "invalid"


def test_v62_migration_adds_the_approval_record_and_converges(tmp_path):
    from tessera_control.migrations import LATEST_SCHEMA_VERSION
    from tessera_control.store import initialize_state, migrate_state

    fresh = tmp_path / "fresh"
    initialize_state(fresh)
    migrated, db_path = _v62_state(tmp_path)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute(
            "UPDATE operator_backup_health SET last_success_at='synthetic',last_backup_id='old'"
        )
        db.commit()
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION == 72
    assert migrate_state(migrated) == LATEST_SCHEMA_VERSION
    names = (
        "snapshot_code_approvals",
        "snapshot_code_approvals_no_update",
        "snapshot_code_approvals_no_delete",
        "snapshot_code_approvals_no_replace",
    )
    query = "SELECT type, sql FROM sqlite_schema WHERE name=?"
    objects = "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY 1, 2"
    with closing(sqlite3.connect(fresh / "tessera.sqlite3")) as expected:
        with closing(sqlite3.connect(db_path)) as actual:
            for name in names:
                assert actual.execute(query, (name,)).fetchone() == (
                    expected.execute(query, (name,)).fetchone()
                )
                assert actual.execute(query, (name,)).fetchone() is not None
            # Every table, index and trigger of a fresh database exists after migration,
            # except the two lineage listing-order indexes, which are deliberately created
            # only with the tables (performance only; older databases sort per query).
            perf_only = {
                ("index", "artifact_lineage_nodes_by_time"),
                ("index", "artifact_lineage_edges_by_time"),
            }
            assert actual.execute(objects).fetchall() == [
                row for row in expected.execute(objects).fetchall() if row not in perf_only
            ]
            assert actual.execute("SELECT count(*) FROM snapshot_code_approvals").fetchone() == (0,)
            assert actual.execute(
                "SELECT last_success_at,last_backup_id FROM operator_backup_health"
            ).fetchone() == (None, None)


def test_live_check_receipt_carries_input_code():
    session = LocalSession()
    session.expects_input = True
    session.input_data_class = lambda route: "public"
    files = [{"relative_path": "main.py", "sha256": "a" * 64}]
    session.input_code = lambda route: {
        "input_code": "private_code_approved",
        "input_code_files": files,
    }
    receipt = drive(session)
    assert receipt["input_code"] == "private_code_approved"
    assert receipt["input_code_files"] == files
    assert receipt["output_data_class"] == "private_derived"
    plain = drive(LocalSession())
    assert plain["input_code"] is None and plain["input_code_files"] is None
    # Input expected but a session that cannot read the code record: unknown, not none.
    blind = LocalSession()
    blind.expects_input = True
    blind.input_data_class = lambda route: "public"
    receipt = drive(blind)
    assert receipt["input_code"] == "unknown" and receipt["input_code_files"] is None
    # Every file approved code: the data class says so instead of null.
    allcode = LocalSession()
    allcode.expects_input = True
    allcode.input_data_class = lambda route: "none_all_approved_code"
    allcode.input_code = lambda route: {
        "input_code": "private_code_approved",
        "input_code_files": files,
    }
    assert drive(allcode)["input_data_class"] == "none_all_approved_code"


def test_worker_run_receipt_carries_input_code(tmp_path):
    from test_execution_run_receipts import _observed_fixture, _report_unresolved, _run_body

    from tessera_control.execution_run_receipts import ExecutionRunReceiptStore

    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    status, receipt = _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    assert status == 201
    assert (receipt["input_code"], receipt["input_code_files"]) == ("none", [])
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        (manifest_json,) = db.execute(
            "SELECT manifest_json FROM source_snapshots WHERE id=?", (grant["snapshot_id"],)
        ).fetchone()
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='worker.execution_run_receipt_recorded'"
            ).fetchone()[0]
        )
    assert (payload["input_code"], payload["input_code_files"]) == ("none", [])
    # ADR 0040 section 4: the receipt and its event name the setting digest in force, the
    # provider instance the worker is bound to (none under the default) and the basis.
    from tessera_control.provider_trust import current_policy

    for record in (receipt, payload):
        assert record["trust_policy_digest"] == current_policy().digest
        assert record["trust_provider_id"] is None
        assert record["admission_basis"] == "provider_trust_policy"  # not recorded public
    entry = json.loads(manifest_json)["entries"][0]
    path = "approved.py"
    # Synthetic fixture: the grant snapshot's first file, recorded as approved code under
    # a code path (the reader checks path, sha256 and size against the manifest).
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE source_snapshots SET manifest_json=? WHERE id=?",
            (
                json.dumps(
                    dict(
                        json.loads(manifest_json),
                        entries=[dict(entry, relative_path=path)]
                        + json.loads(manifest_json)["entries"][1:],
                    )
                ),
                grant["snapshot_id"],
            ),
        )
        db.execute(
            """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
            actor_principal_id, decision_class, provenance_json, recorded_at)
            VALUES (?, ?, ?, 'human_session', '{}', '2026-01-01T00:00:00Z')""",
            (
                grant["snapshot_id"],
                json.dumps(
                    [{"relative_path": path, "sha256": entry["sha256"], "size": entry["size"]}]
                ),
                owner["id"],
            ),
        )
    read = ExecutionRunReceiptStore(state).get_run_receipt(owner, checkpoint["operation_id"])
    assert read["input_code"] == "private_code_approved"
    assert read["input_data_class"] == "none_all_approved_code"
    assert read["input_code_files"] == [{"relative_path": path, "sha256": entry["sha256"]}]
    assert read["output_data_class"] == "private_derived"


# ---------------------------------------------------------------------- setup script


def _stage_smoke(root):
    from test_gpu_runtime_entry_input import smoke_stage

    smoke_stage.stage(root / "smoke-input", root / "smoke-contract.json",
                      "custody.example-tailnet.ts.net")  # fmt: skip
    return root / "smoke-input", root / "smoke-contract.json"


def _smoke_argv(inputs, extra):  # noqa: F811
    directory, contract = _stage_smoke(inputs["root"])
    argv = inputs["argv"] + ["--workload-contract", str(contract), "--input-dir", str(directory)]
    return argv + extra, directory


def _nothing_written(inputs):  # noqa: F811
    assert not inputs["output"].exists()
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM snapshot_code_approvals").fetchone()[0] == 0


def test_setup_stages_approved_code_with_public_data_and_the_route_pulls_it(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
    trust_any_worker,
):
    from tessera_control.provider_inputs import TicketInvalid
    from tessera_control.remote_execution_models import RemoteProposal

    argv, directory = _smoke_argv(
        inputs,
        [
            "--data-class", "public", "--license", "CC0-1.0",
            "--source", "tessera:workloads/gpu_runtime_v1/smoke/stage.py",
            "--approve-private-code", "entry.py",
        ],
    )  # fmt: skip
    # The setup enrols its own executor, so bind whichever principal it creates to salad.
    trust_any_worker("salad", CODE_CLASSES)
    assert setup_script.main(argv) == 0, capsys.readouterr().err
    setup = json.loads(capsys.readouterr().out)
    entry_sha = _sha((directory / "entry.py").read_bytes())
    basis = next(r for r in setup["review_required"] if r["field"] == "selection.manifests.data")[
        "basis"
    ]
    assert "recorded data class public" in basis and "license CC0-1.0" in basis
    assert "private code approved for this remote run, not public" in basis
    assert f"entry.py sha256 {entry_sha}" in basis
    assert "decision_class owner_automation_labelled" in basis
    session = open_session(Path(setup["config_file"]), monkeypatch)
    route = setup["records"]["route_id"]
    try:
        state = session.state(route)
        proposal = RemoteProposal.model_validate(state["proposal"])
        server = session.server
        request = {"lease_id": proposal.lease_id, "expected_epoch": proposal.epoch}
        server.store.preflight_authority(server.worker, route, proposal, "create")
        ticket = server.inputs.issue_ticket(server.worker, proposal.operation_id, request)
        pulled = server.inputs.read_manifest(ticket["token"])
        assert {e["relative_path"] for e in pulled["manifest"]["entries"]} >= {"entry.py"}
        for chunk in pulled["chunks"]:
            server.inputs.read_chunk_for_ticket(ticket["token"], chunk["seq"])
        assert session.input_data_class(route) == "public"
        assert session.input_code(route) == {
            "input_code": "private_code_approved",
            "input_code_files": [{"relative_path": "entry.py", "sha256": entry_sha}],
        }
        # Approved code never makes the other files admissible: once the data is not
        # public, admission, ticket issue and every pull refuse.
        with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
            rewrite_class_row(db, ticket["snapshot_id"])
            db.execute(
                "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,recorded_at) "
                "VALUES (?,'private_derived','default','2026-01-01T00:00:00Z')",
                (ticket["snapshot_id"],),
            )
            restore_class_guards(db)
        with pytest.raises(TicketInvalid):
            server.inputs.read_manifest(ticket["token"])
        with pytest.raises(Conflict, match="public"):
            server.store.preflight_authority(server.worker, route, proposal, "create")
        with pytest.raises(Forbidden, match="public"):
            server.inputs.issue_ticket(server.worker, proposal.operation_id, request)
    finally:
        session.close()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        # entry.py is code but not approved, and there is no public declaration.
        (
            [
                "--approve-private-code",
                "expected.json",
                "--confirm-structured-config",
                "expected.json",
            ],
            "--data-class public",
        ),
        # Non-code extension.
        (["--approve-private-code", "data/sample.bin", *PUBLIC_INPUT], "code or config"),
        # Not a file of the input.
        (["--approve-private-code", "absent.py", *PUBLIC_INPUT], "not a file of the input"),
        # The entry must be approved code once any code is approved.
        (
            ["--approve-private-code", "expected.json", *PUBLIC_INPUT],
            "structured and can hold data",
        ),
        (
            [
                "--approve-private-code",
                "expected.json",
                "--confirm-structured-config",
                "expected.json",
                *PUBLIC_INPUT,
            ],
            "not approved private",
        ),
    ],
)
def test_setup_refuses_before_any_write(inputs, capsys, extra, message):  # noqa: F811
    argv, _ = _smoke_argv(inputs, extra)
    assert setup_script.main(argv) == 1
    assert message in capsys.readouterr().err
    _nothing_written(inputs)


def test_setup_refuses_approved_code_without_an_input_dir(inputs, capsys):  # noqa: F811
    assert setup_script.main(inputs["argv"] + ["--approve-private-code", "a.py"]) == 1
    assert "apply only to --input-dir" in capsys.readouterr().err
    _nothing_written(inputs)


def test_setup_refuses_data_files_that_are_neither_approved_nor_public(
    inputs,  # noqa: F811
    capsys,
):
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, _ = setup_contract(directory, raw)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + [
        "--workload-contract", str(path), "--input-dir", str(directory),
        "--approve-private-code", "runtime/manifest.json",
        "--confirm-structured-config", "runtime/manifest.json",
    ]  # fmt: skip
    assert setup_script.main(argv) == 1
    err = capsys.readouterr().err
    assert "--data-class public" in err and "not approved code: data.jsonl" in err
    _nothing_written(inputs)
