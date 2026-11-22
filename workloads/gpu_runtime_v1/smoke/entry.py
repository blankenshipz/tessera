"""Generic gpu_runtime_v1 cold-start smoke entry; stdlib only, delivered in the staged input.

Run by the runtime as ``python -B entry.py WORK_DIR OUT_DIR`` from the read-only
input tree. It names no experiment, model or dataset. It:

1. reads the small data file from ``WORK_DIR/input`` and computes its sha256
   (compared with the digest the staging helper recorded next to it);
2. probes the GPU through ctypes against the host driver's ``libcuda.so.1``
   (cuInit, cuDeviceGetCount, cuDeviceGetName, cuDeviceTotalMem), recording a
   closed error instead of failing when the driver is unavailable;
3. writes the two declared outputs: ``result.json`` (input digest, GPU facts,
   timings) and ``probe.txt`` (a short human-readable summary).

The probe never raises: a missing driver or a CUDA error is a recorded fact, and
the outputs are still produced, so a run proves delivery, input and custody even
on a node whose GPU is not usable. Exit status is non-zero only when the input
cannot be read or an output cannot be written.
"""

import ctypes
import hashlib
import json
import os
import platform
import sys
import time

SCHEMA = "tessera.gpu-runtime-smoke.v1"
DATA_PATH = "data/sample.bin"
EXPECTED_PATH = "expected.json"
RESULT = "result.json"
PROBE = "probe.txt"
MAX_DEVICES = 16
MAX_OUTPUT_BYTES = 65536  # each declared output's cap in the smoke contract
MAX_DATA_BYTES = 1 << 20
CUDA_LIBRARY = "libcuda.so.1"


def _read_input(work):
    root = os.path.join(work, "input")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.path.join(root, *DATA_PATH.split("/")), flags)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_DATA_BYTES + 1)
    if len(data) > MAX_DATA_BYTES:
        raise ValueError("input data exceeds the smoke cap")
    expected = None
    try:
        with open(os.path.join(root, EXPECTED_PATH), "rb") as handle:
            value = json.loads(handle.read(4096))
        expected = value.get("data_sha256") if isinstance(value, dict) else None
    except (OSError, ValueError):
        expected = None
    digest = hashlib.sha256(data).hexdigest()
    return {
        "path": DATA_PATH,
        "bytes": len(data),
        "sha256": digest,
        "expected_sha256": expected,
        "matches_expected": expected == digest,
    }


def probe_gpu(loader=ctypes.CDLL):
    """GPU facts from the host driver, or ``available: false`` with a closed error code."""
    facts = {"library": CUDA_LIBRARY, "available": False, "error": None, "devices": []}
    started = time.monotonic()
    try:
        try:
            cuda = loader(CUDA_LIBRARY)
        except OSError:
            facts["error"] = "library_unavailable"
            return facts
        try:
            init = cuda.cuInit
            get_count = cuda.cuDeviceGetCount
            get_device = cuda.cuDeviceGet
            get_name = cuda.cuDeviceGetName
            total_mem = getattr(cuda, "cuDeviceTotalMem_v2", None) or cuda.cuDeviceTotalMem
        except AttributeError:
            facts["error"] = "symbol_missing"
            return facts
        version = ctypes.c_int(0)
        get_version = getattr(cuda, "cuDriverGetVersion", None)
        if get_version is not None and get_version(ctypes.byref(version)) == 0:
            facts["driver_version"] = version.value
        code = init(0)
        if code != 0:
            facts["error"] = f"cuInit:{int(code)}"
            return facts
        count = ctypes.c_int(0)
        code = get_count(ctypes.byref(count))
        if code != 0:
            facts["error"] = f"cuDeviceGetCount:{int(code)}"
            return facts
        facts["device_count"] = count.value
        for ordinal in range(min(max(count.value, 0), MAX_DEVICES)):
            device = ctypes.c_int(0)
            item = {"ordinal": ordinal}
            code = get_device(ctypes.byref(device), ordinal)
            if code != 0:
                item["error"] = f"cuDeviceGet:{int(code)}"
                facts["devices"].append(item)
                continue
            name = ctypes.create_string_buffer(256)
            code = get_name(name, 256, device)
            if code == 0:
                item["name"] = name.value.decode("utf-8", "replace")[:255]
            else:
                item["error"] = f"cuDeviceGetName:{int(code)}"
            memory = ctypes.c_size_t(0)
            code = total_mem(ctypes.byref(memory), device)
            if code == 0:
                item["total_memory_bytes"] = memory.value
            else:
                item["error"] = f"cuDeviceTotalMem:{int(code)}"
            facts["devices"].append(item)
        facts["available"] = count.value > 0 and all("error" not in d for d in facts["devices"])
        if count.value == 0:
            facts["error"] = "no_devices"
        return facts
    except Exception:
        facts["available"] = False
        facts["error"] = facts["error"] or "probe_failed"
        return facts
    finally:
        facts["probe_seconds"] = round(time.monotonic() - started, 6)


def _write(out_dir, name, data):
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("output exceeds its declared cap")
    path = os.path.join(out_dir, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def summary_text(result):
    gpu = result["gpu"]
    lines = [
        f"schema {result['schema']}",
        f"input {result['input']['path']} {result['input']['bytes']} bytes "
        f"sha256 {result['input']['sha256']} matches_expected "
        f"{str(result['input']['matches_expected']).lower()}",
        f"gpu available {str(gpu['available']).lower()} error {gpu['error'] or 'none'}",
    ]
    for device in gpu["devices"]:
        lines.append(
            f"device {device['ordinal']} {device.get('name', '?')} "
            f"memory {device.get('total_memory_bytes', '?')} bytes"
        )
    lines.append(f"total_seconds {result['timings']['total_seconds']}")
    return ("\n".join(lines) + "\n").encode("utf-8", "replace")


def main(argv, loader=ctypes.CDLL):
    if len(argv) != 2:
        return 2
    work, out_dir = argv
    started = time.monotonic()
    wall = time.time()
    data = _read_input(work)
    read_done = time.monotonic()
    gpu = probe_gpu(loader)
    result = {
        "schema": SCHEMA,
        "input": data,
        "gpu": gpu,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "timings": {
            "started_unix": round(wall, 3),
            "input_seconds": round(read_done - started, 6),
            "probe_seconds": gpu["probe_seconds"],
            "total_seconds": round(time.monotonic() - started, 6),
        },
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    _write(out_dir, RESULT, encoded)
    _write(out_dir, PROBE, summary_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
