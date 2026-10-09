"""Build-only exact-source slicing: no control Store/API in workload image.

Preserve actual pure verifier function definitions verbatim; refuse unknown runtime
top-level nodes. The backend still owns the canonical module. This is not a stub
or an alternate verification implementation. Candidate inventory retains both
canonical source hash and the exact sliced workload file hash.
"""

import ast
import hashlib
import json
from pathlib import Path


def slice_verifier(path):
    source = path.read_text()
    tree = ast.parse(source)
    excluded = {"snapshots", "store"}
    kept = []
    required = {"verify_dataset_bytes", "load_dataset_snapshot", "manifest_bytes", "_read_file"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in excluded:
            continue
        if isinstance(node, ast.ClassDef) and node.name == "DatasetSnapshotStore":
            continue
        if isinstance(node, ast.FunctionDef):
            found.add(node.name)
        elif not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Expr, ast.Assign)):
            raise ValueError("Unexpected top-level source; require reviewed portable boundary")
        if isinstance(node, ast.ClassDef) and node.name != "DatasetIntegrityError":
            raise ValueError("Unexpected verifier class")
        kept.append(ast.get_source_segment(source, node))
    if not required <= found:
        raise ValueError("Required actual verifier definition missing")
    return ("\n\n".join(kept) + "\n").encode()


if __name__ == "__main__":
    source = Path("/app/tessera_control/dataset_snapshots.py")
    original = source.read_bytes()
    portable = slice_verifier(source)
    source.write_bytes(portable)
    Path("/app/verifier-source.json").write_text(
        json.dumps(
            {
                "canonical_source_sha256": hashlib.sha256(original).hexdigest(),
                "portable_source_sha256": hashlib.sha256(portable).hexdigest(),
                "method": "verbatim-pure-function-definitions-no-Store-API",
            },
            sort_keys=True,
        )
    )
