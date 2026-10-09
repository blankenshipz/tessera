"""Identity contracts for disabled future adapters; declarations are NOT privileges.

Hash the entire native target contract into Assignment.workload.instance_digest.
An implementation must retain the native IDs as well as validating the digest;
it must never use labels/names alone for stop or substitute these declarations for
independently measured CNI/CRI/cgroup/filesystem or provider evidence.
"""

from typing import Literal

from pydantic import Field

from .protocol import Digest, Frozen, Name


class KubernetesMonitorTarget(Frozen):
    adapter: Literal["kubernetes-v1"] = "kubernetes-v1"
    enabled: Literal[False] = False
    cluster_digest: Digest
    node_uid: Name
    node_boot_id: Name
    namespace_uid: Name
    job_uid: Name
    job_resource_version: Name
    pod_uid: Name
    pod_resource_version: Name
    container_id: Digest
    cgroup_generation: Digest
    service_account_uid: Name
    native_sensor_identity: Name
    native_sensor_certificate_sha256: Digest
    authority: Literal["exact-uid-resourceversion-stop-only"] = (
        "exact-uid-resourceversion-stop-only"
    )


class SaladMonitorTarget(Frozen):
    adapter: Literal["salad-v1"] = "salad-v1"
    enabled: Literal[False] = False
    organization: Name
    project: Name
    container_group_id: Name
    allocation_id: Name
    allocation_generation: int = Field(ge=1, le=2**53)
    worker_identity: Name
    worker_boot_id: Name
    image_digest: Digest
    native_sensor_identity: Name
    native_sensor_certificate_sha256: Digest
    cancellation: Literal["unimplemented"] = "unimplemented"
    billing: Literal["unknown"] = "unknown"
