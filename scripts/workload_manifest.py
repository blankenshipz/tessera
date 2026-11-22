"""Build the gpu_runtime_v1 fetch manifest from a reviewed WorkloadContractV1 JSON.

Offline and generic. Each contract `egress.fetches` entry, in order, is paired
with one `--item KIND:DEST` (KIND wheel|file|entry; DEST a relative name). The
manifest carries exactly the contract's url/sha256/max_bytes, so what the
runtime fetches equals what the contract pins. `--item KIND:input:PATH` (KIND
entry|file) instead names a file inside the staged input snapshot: it pairs with
no fetch, carries no url/sha256/max_bytes and is pinned by the snapshot digest.
Prints the manifest sha256 (the value that goes into the reviewed argv_template);
`--out` writes the canonical bytes and `--refs` prints the `b64:` argv tokens
for an inline manifest.

For a manifest carried inside the staged input snapshot, write it with `--out`
into the input directory, then `--input-ref PATH` prints the
`input-manifest:FETCHES_SHA256:PATH` token, and `--input-dir DIR` prints the
input snapshot digest and total bytes for the contract's `input` declaration
(and the `input:` token, using the total as MAX_BYTES unless `--input-max-bytes`).

A contract whose `egress.redirect_hosts` is non-empty gets the same list in the
manifest (`redirect_hosts`); `--redirect-ref` prints the matching
`redirect-hosts:HOSTS_SHA256` argv token, which goes after the optional `input:`
token and before the manifest refs. Redirect hosts need `runtime: gpu_runtime_v1`,
whose argv needs this helper's output, so for a draft without `runtime` the
helper validates the rest of the contract and its `egress` (with
`redirect_hosts`) separately.
"""

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCHEMA = "tessera.gpu-runtime.manifest.v1"
KINDS = ("wheel", "file", "entry")
CHUNK = 240


INPUT_PREFIX = "input:"


def build_manifest(contract, items):
    fetches = list(contract.egress.fetches)
    url_items = [i for i in items if not i.partition(":")[2].startswith(INPUT_PREFIX)]
    if len(url_items) != len(fetches):
        raise ValueError("one KIND:DEST item is required per contract fetch, in order")
    rows = []
    for item in items:
        kind, sep, dest = item.partition(":")
        if dest.startswith(INPUT_PREFIX):
            path = dest[len(INPUT_PREFIX) :]
            if kind not in ("entry", "file") or not path:
                raise ValueError("input item must be entry:input:PATH or file:input:PATH")
            rows.append({"kind": kind, "source": "input", "path": path})
            continue
        if not sep or kind not in KINDS or not dest:
            raise ValueError("item must be KIND:DEST with KIND in wheel|file|entry")
        fetch = fetches.pop(0)
        rows.append(
            {
                "url": fetch.url,
                "sha256": fetch.sha256,
                "max_bytes": fetch.max_bytes,
                "kind": kind,
                "dest": dest,
            }
        )
    manifest = {"schema": SCHEMA, "fetches": rows}
    hosts = list(contract.egress.redirect_hosts)
    if hosts:
        manifest["redirect_hosts"] = hosts
    from tessera_control.salad_workload_contract import check_gpu_runtime_manifest_rules

    # Keys, shape, .py entry, unique paths, exactly one entry. The reviewed contract
    # (runtime gpu_runtime_v1) separately requires its declared input.
    check_gpu_runtime_manifest_rules(manifest, input_declared=True, redirect_hosts=hosts)
    return manifest


def load_contract(raw):
    """The validated contract; a draft without runtime gets its egress validated alone."""
    from tessera_control.salad_workload_contract import Egress, WorkloadContractV1

    data = json.loads(raw)
    egress = data.get("egress") if isinstance(data, dict) else None
    if isinstance(egress, dict) and egress.get("redirect_hosts") and data.get("runtime") is None:
        rest = {k: v for k, v in egress.items() if k != "redirect_hosts"}
        WorkloadContractV1.model_validate({**data, "egress": rest})
        return SimpleNamespace(egress=Egress.model_validate(egress))
    return WorkloadContractV1.model_validate(data)


def rows_of(manifest):
    """Input-sourced items are no fetch: they are absent from FETCHES_SHA256 and egress."""
    return [
        (f["url"], f["sha256"], f["max_bytes"]) for f in manifest["fetches"] if "source" not in f
    ]


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def refs(raw):
    text = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return ["b64:" + text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path, help="WorkloadContractV1 JSON file")
    parser.add_argument("--item", action="append", default=[], required=True)
    parser.add_argument("--out", type=Path, help="write canonical manifest bytes here")
    parser.add_argument("--refs", action="store_true", help="also print b64: argv tokens")
    parser.add_argument("--input-ref", help="print the input-manifest: token for this PATH")
    parser.add_argument(
        "--redirect-ref", action="store_true", help="print the redirect-hosts: argv token"
    )
    parser.add_argument("--input-dir", type=Path, help="print the staged input digest")
    parser.add_argument("--input-max-bytes", type=int, default=None)
    args = parser.parse_args(argv)
    from tessera_control.salad_workload_contract import (
        REDIRECT_TOKEN_PREFIX,
        fetches_digest,
        redirect_hosts_digest,
    )

    contract = load_contract(args.contract.read_bytes())
    if args.redirect_ref and not contract.egress.redirect_hosts:
        parser.error("--redirect-ref needs a contract with egress.redirect_hosts")
    raw = canonical(build_manifest(contract, args.item))
    if args.out:
        args.out.write_bytes(raw)
    print(hashlib.sha256(raw).hexdigest())
    if args.refs:
        print("\n".join(refs(raw)))
    if args.redirect_ref:
        print(REDIRECT_TOKEN_PREFIX + redirect_hosts_digest(contract.egress.redirect_hosts))
    if args.input_ref:
        rows = rows_of(json.loads(raw))
        print(f"input-manifest:{fetches_digest(rows)}:{args.input_ref}")
    if args.input_dir:
        from tessera_control.salad_workload_input import staged_input_manifest

        manifest, digest = staged_input_manifest(args.input_dir.resolve())
        cap = args.input_max_bytes or max(1, manifest["total_bytes"])
        print(json.dumps({"snapshot_digest": digest, "total_bytes": manifest["total_bytes"]}))
        print(f"input:{digest}:{cap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
