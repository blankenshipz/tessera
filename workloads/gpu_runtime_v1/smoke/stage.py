"""Write the gpu_runtime_v1 cold-start smoke's staged input and its workload contract.

Offline and generic: no network access, no URL fetches, no wheels. The staged
input directory holds the stdlib-only entry (``entry.py``), a small
deterministic data file and its expected digest; the contract declares that
directory as its input snapshot and serves the entry from it with an inline
manifest whose only item is ``{"kind": "entry", "source": "input", "path":
"entry.py"}``. ``egress.fetches`` is empty, so the route's capability network is
empty too.

    python workloads/gpu_runtime_v1/smoke/stage.py \\
        --input-dir /ABS/new-input-dir --contract-out /ABS/contract.json \\
        --custody-host CUSTODY.ts.net [--watchdog-profile 64HEX]

Both paths must not exist; the directory is created 0700 and the contract 0600,
as ``scripts/salad_live_setup.py --workload-contract/--input-dir`` require. The
contract is validated (and the directory checked against it exactly as setup
does) before it is written. Prints a JSON summary with the snapshot digest and
the manifest sha256. ``--watchdog-profile`` must equal the value later passed to
setup (setup's default is 64 ``b``).
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY_SOURCE = HERE / "entry.py"
ENTRY_PATH = "entry.py"
DATA_PATH = "data/sample.bin"
EXPECTED_PATH = "expected.json"
MANIFEST_SCHEMA = "tessera.gpu-runtime.manifest.v1"
WORKLOAD_SECONDS = 300
OUTPUTS = [{"name": "result.json", "max_bytes": 65536}, {"name": "probe.txt", "max_bytes": 65536}]
DEFAULT_WATCHDOG = "b" * 64  # salad_live_setup.py's --watchdog-profile default


def sample_data():
    """4 KiB of fresh random bytes, new on every staging.

    Synthetic and public (declared CC0-1.0 at setup). Fresh bytes keep each smoke's
    input digest unique, so ADR 0039's anti-laundering rule (bytes already recorded
    derived or migrated can never be declared public) never blocks a rerun.
    """
    return os.urandom(4096)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def manifest_bytes():
    item = {"kind": "entry", "source": "input", "path": ENTRY_PATH}
    return canonical({"schema": MANIFEST_SCHEMA, "fetches": [item]})


def manifest_refs(raw, chunk=240):
    text = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return ["b64:" + text[i : i + chunk] for i in range(0, len(text), chunk)]


def write_input_dir(directory):
    """Create ``directory`` (0700) with the entry, the data file and its expected digest.

    A pre-existing ``directory`` makes ``os.mkdir`` fail and is never touched. Once
    our own ``mkdir`` succeeded, any later failure removes the partial directory.
    """
    data = sample_data()
    os.mkdir(directory, 0o700)  # raises (and removes nothing) if it already exists
    try:
        os.mkdir(directory / "data", 0o700)
        files = {
            ENTRY_PATH: ENTRY_SOURCE.read_bytes(),
            DATA_PATH: data,
            EXPECTED_PATH: canonical({"data_sha256": hashlib.sha256(data).hexdigest()}),
        }
        for relative, content in files.items():
            fd = os.open(directory / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)  # only the directory we created
        raise
    return hashlib.sha256(data).hexdigest()


def build_contract(custody_host, snapshot_digest, max_bytes, watchdog=DEFAULT_WATCHDOG):
    raw = manifest_bytes()
    argv = [
        "v1", "{operation}", "{epoch}", "{custody_host}", watchdog, str(WORKLOAD_SECONDS),
        str(sum(o["max_bytes"] for o in OUTPUTS)), str(len(OUTPUTS)),
        hashlib.sha256(raw).hexdigest(), f"input:{snapshot_digest}:{max_bytes}",
        *manifest_refs(raw),
    ]  # fmt: skip
    return {
        "contract": "tessera.salad.workload.v1",
        "runtime": "gpu_runtime_v1",
        "argv_template": argv,
        "env": {},
        "workload_seconds": WORKLOAD_SECONDS,
        "outputs": OUTPUTS,
        "egress": {"kind": "provider_broad", "fetches": []},
        "custody_host": custody_host,
        "input": {
            "kind": "staged_snapshot",
            "snapshot_digest": snapshot_digest,
            "max_bytes": max_bytes,
        },
    }


def stage(input_dir, contract_out, custody_host, watchdog=DEFAULT_WATCHDOG):
    """Write both artifacts; return the summary. Refuses existing paths."""
    from tessera_control.salad_workload_contract import WorkloadContractV1
    from tessera_control.salad_workload_input import check_staged_input, staged_input_manifest

    for path in (input_dir, contract_out):
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("paths must be explicit absolute paths")
        if os.path.lexists(path):
            raise ValueError(f"{path} already exists; use a new path")
    data_sha256 = write_input_dir(input_dir)  # cleans up after its own failures
    try:
        manifest, digest = staged_input_manifest(input_dir)
        value = build_contract(custody_host, digest, max(1, manifest["total_bytes"]), watchdog)
        contract = WorkloadContractV1.model_validate(value)
        check_staged_input(contract, input_dir)
        fd = os.open(contract_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(contract.model_dump(mode="json")) + b"\n")
    except BaseException:
        shutil.rmtree(input_dir, ignore_errors=True)  # ours: write_input_dir created it
        raise
    return {
        "contract": str(contract_out),
        "input_dir": str(input_dir),
        "snapshot_digest": digest,
        "input_bytes": manifest["total_bytes"],
        "manifest_sha256": value["argv_template"][8],
        "data_sha256": data_sha256,
        "outputs": [o["name"] for o in OUTPUTS],
        "workload_seconds": WORKLOAD_SECONDS,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--contract-out", type=Path, required=True)
    parser.add_argument("--custody-host", required=True, help="equals setup's --custody-dns")
    parser.add_argument("--watchdog-profile", default=DEFAULT_WATCHDOG)
    args = parser.parse_args(argv)
    try:
        summary = stage(args.input_dir, args.contract_out, args.custody_host, args.watchdog_profile)
    except (OSError, ValueError) as error:
        print(f"smoke staging refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
