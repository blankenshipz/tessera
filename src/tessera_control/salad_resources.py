"""Salad container-group resource shape check for new work (admission, setup, transport).

Source: the container-group create and get pages of the Salad public API reference
(docs.salad.com, "Create Container Group" / "Get Container Group", container
``resources``). Their prose limits are stricter than the published OpenAPI schema,
so the prose limits apply here:

* ``cpu``: whole vCPUs, 1..16.
* ``memory``: MB, 1024..61440.
* ``storage_amount``: bytes, a whole number of GiB, 1 GiB..250 GiB.
* ``gpu_classes``: GPU class UUIDs (none, or exactly one here; the live transport
  additionally requires exactly one).

Tessera expresses these as ``cpu_millicores`` (cores x 1000), ``memory_mb`` and
``disk_mb`` (MiB; the transport sends ``disk_mb * 1024 * 1024`` bytes).

This runs where new work is created (setup, proposal, approval, the create action
and the transport before its POST), never at start of an existing stopped group and
never in a model validator: a stored proposal must keep parsing so stop, status and
reconcile work even when a stored shape is outside these limits.
"""

import re

GIB_MIB = 1024
MIN_CPU_CORES, MAX_CPU_CORES = 1, 16
MIN_MEMORY_MB, MAX_MEMORY_MB = 1024, 61440
MIN_STORAGE_GIB, MAX_STORAGE_GIB = 1, 250
_UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


def salad_resource_refusal(*, cpu_millicores, memory_mb, disk_mb, gpu_class, gpu_count):
    """Return ``(field, message)`` for the first refused value, or None when admissible."""
    if type(cpu_millicores) is not int or cpu_millicores % 1000:
        return "cpu_millicores", "cpu must be a whole number of vCPUs (a multiple of 1000)"
    if not MIN_CPU_CORES <= cpu_millicores // 1000 <= MAX_CPU_CORES:
        return "cpu_millicores", (
            f"cpu must be {MIN_CPU_CORES}..{MAX_CPU_CORES} vCPUs "
            f"({MIN_CPU_CORES * 1000}..{MAX_CPU_CORES * 1000} millicores)"
        )
    if type(memory_mb) is not int or not MIN_MEMORY_MB <= memory_mb <= MAX_MEMORY_MB:
        return "memory_mb", f"memory must be {MIN_MEMORY_MB}..{MAX_MEMORY_MB} MB"
    if type(disk_mb) is not int or disk_mb % GIB_MIB:
        return "disk_mb", (
            f"storage must be a whole number of GiB (a multiple of {GIB_MIB} MiB); {disk_mb} is not"
        )
    if not MIN_STORAGE_GIB <= disk_mb // GIB_MIB <= MAX_STORAGE_GIB:
        return "disk_mb", (
            f"storage must be {MIN_STORAGE_GIB}..{MAX_STORAGE_GIB} GiB "
            f"({MIN_STORAGE_GIB * GIB_MIB}..{MAX_STORAGE_GIB * GIB_MIB} MiB)"
        )
    if gpu_count == 0 and gpu_class is None:
        return None  # CPU-only shape: an empty gpu_classes list
    if gpu_count != 1:
        return "gpu_count", "at most one GPU class is supported"
    if type(gpu_class) is not str or not _UUID.fullmatch(gpu_class):
        return "gpu_class", "the GPU class must be a lowercase UUID"
    return None


def check_salad_resources(resources):
    """Raise ValueError when a selection's ``resources`` cannot be a Salad create."""
    refusal = salad_resource_refusal(
        cpu_millicores=resources.cpu_millicores,
        memory_mb=resources.memory_mb,
        disk_mb=resources.disk_mb,
        gpu_class=resources.gpu_class,
        gpu_count=resources.gpu_count,
    )
    if refusal is not None:
        raise ValueError(f"Salad resources refused: {refusal[1]}")
