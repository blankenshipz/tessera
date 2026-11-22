"""Scripted remote observations plus real disposable local directory checks."""

import copy
import json
import os
import shutil

import pytest

from tessera_operator import maintenance_topology as topology
from tessera_operator.schema_supervised import SupervisedDenied


@pytest.fixture
def scene(tmp_path, monkeypatch):
    source = tmp_path.resolve() / "state"
    monkeypatch.setattr(topology.target, "SOURCE", source)
    root = source / "private/local-control-upgrade/supervised/test"
    for phase in ("backup", "validate"):
        (root / "custody" / phase).mkdir(parents=True, mode=0o700)
    plan = dict(
        operation="test",
        deadline=200,
        target={k + "_uid": k for k in ("namespace", "deployment", "pvc", "pv")},
    )
    volume = dict(name="state", persistentVolumeClaim=dict(claimName="tessera-control-state"))

    def obj(kind, **fields):
        return dict(metadata=dict(uid=kind, resourceVersion="1"), **fields)

    def owner(uid):
        return dict(uid=uid, controller=True)

    pod = obj("pod", spec=dict(nodeName=topology.NODE, volumes=[volume]))
    pod["metadata"]["ownerReferences"] = [owner("rs")]
    rs = obj("rs")
    rs["metadata"]["ownerReferences"] = [owner("deployment")]
    resources = {
        "namespace": obj("namespace"),
        "node": obj("node", status=dict(nodeInfo=dict(machineID="c" * 32))),
        "deployment": obj(
            "deployment",
            spec=dict(template=dict(spec=dict(volumes=[volume], env="NOT-IN-RECEIPT"))),
        ),
        "pvc": obj("pvc", spec=dict(volumeName="pvc-" + "a" * 36), status=dict(phase="Bound")),
        "pv": obj(
            "pv",
            spec=dict(
                hostPath=dict(path="/state/data"),
                claimRef=dict(
                    uid="pvc", name="tessera-control-state", namespace=topology.target.NAMESPACE
                ),
                nodeAffinity=dict(
                    required=dict(
                        nodeSelectorTerms=[
                            dict(
                                matchExpressions=[
                                    dict(
                                        key="kubernetes.io/hostname",
                                        operator="In",
                                        values=[topology.NODE],
                                    )
                                ]
                            )
                        ]
                    )
                ),
            ),
        ),
        "pods": dict(items=[pod]),
        "replicasets": dict(items=[rs]),
    }
    node = dict(
        id="b" * 64,
        pid=1,
        mounts=[
            dict(Type="volume", Source="/var/lib/docker/volumes/node/_data", Destination="/state")
        ],
    )
    calls = []
    mountinfo = "1 0 8:1 / /state rw - ext4 /dev/vda rw\n"
    hook = [None]

    def execute(argv, *, timeout, max_bytes):
        assert timeout == 10 and max_bytes == topology.LIMIT
        calls.append(argv)
        if hook[0]:
            result = hook[0](argv, len(calls))
            if result is not None:
                return result
        if argv[0] == "kubectl":
            assert argv[:5] == (
                "kubectl",
                "--kubeconfig",
                str(topology.target.KUBECONFIG),
                "--context",
                topology.target.CONTEXT,
            )
            return json.dumps(resources[argv[argv.index("get") + 1]])
        assert argv[:3] == ("docker", "--context", "desktop-linux")
        if argv[3] == "inspect":
            assert argv[4:] == ("--format", topology.INSPECT, topology.NODE)
            return "\n".join(json.dumps(node[k]) for k in ("id", "pid", "mounts"))
        assert argv[3:5] == ("exec", "b" * 64)
        if argv[5:] == ("cat", "/etc/machine-id"):
            return "c" * 32 + "\n"
        if argv[5] == "readlink":
            assert argv[6:] == ("-e", "--", "/state/data")
            return "/state/data\n"
        if argv[5] == "stat":
            assert argv[6:] == ("-c", "%d:%i:%f:%Z", "--", "/state/data", "/state", "/")
            return "8:1:41c0:1\n8:2:41c0:1\n8:3:41c0:1\n"
        assert argv[5:] == ("cat", "/proc/self/mountinfo")
        return mountinfo

    collector = topology.TopologyCollector(execute, clock=lambda: 100)
    control = dict(name="control", volumeMounts=[dict(name="state", mountPath="/state")])
    pod["spec"]["containers"] = [copy.deepcopy(control)]
    resources["deployment"]["spec"]["template"]["spec"]["containers"] = [control]
    return dict(
        collector=collector,
        plan=plan,
        root=root,
        resources=resources,
        node=node,
        calls=calls,
        hook=hook,
        execute=execute,
    )


def test_collect_pins_chain_provenance_and_exclusive_receipt(scene):
    result = scene["collector"].collect(scene["plan"])
    assert result["failure_domain"] == "maintenance-pod-and-pvc"
    assert result["valid_until"] == 160
    assert result["backing"]["kind"] == "vm-local-volume"
    assert len(result["observations"]) == len(scene["calls"])
    assert result["topology"]["pod"]["metadata"]["uid"] == "pod"
    raw = (scene["root"] / "topology.json").read_text()
    assert "NOT-IN-RECEIPT" not in raw
    assert os.stat(scene["root"] / "topology.json").st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        scene["collector"].collect(scene["plan"])


@pytest.mark.parametrize(
    "change",
    [
        "pvc",
        "claim",
        "volume",
        "node",
        "owner",
        "affinity",
        "container",
        "mapping",
        "overlay",
        "machine",
    ],
)
def test_identity_and_unknown_mapping_deny_without_receipt(scene, change):
    r = scene["resources"]
    if change == "pvc":
        r["pvc"]["metadata"]["uid"] = "stale"
    elif change == "claim":
        r["pv"]["spec"]["claimRef"]["uid"] = "other"
    elif change == "volume":
        r["deployment"]["spec"]["template"]["spec"]["volumes"] = []
    elif change == "node":
        r["pods"]["items"][0]["spec"]["nodeName"] = "other"
    elif change == "owner":
        r["replicasets"]["items"][0]["metadata"]["ownerReferences"] = []
    elif change == "affinity":
        r["pv"]["spec"]["nodeAffinity"] = {}
    elif change == "container":
        scene["node"]["pid"] = 0
    elif change == "mapping":
        scene["node"]["mounts"][0]["Type"] = "bind"
    elif change == "machine":
        r["node"]["status"]["nodeInfo"]["machineID"] = "d" * 32
    else:
        scene["node"]["mounts"] = []
    with pytest.raises(SupervisedDenied):
        scene["collector"].collect(scene["plan"])
    assert not (scene["root"] / "topology.json").exists()


def test_shared_mac_alias_denied(scene):
    mapping = scene["node"]["mounts"][0]
    mapping.update(Type="bind", Source="/host_mnt" + str(scene["root"] / "custody"))
    # /state/data translates to custody/data, make it the exact backup alias.
    mapping["Destination"] = "/state/data"
    mapping["Source"] += "/backup"
    scene["hook"][0] = lambda a, n: (
        "1 0 0:1 / /state/data rw - virtiofs shared rw\n"
        if a[-1] == "/proc/self/mountinfo"
        else None
    )
    with pytest.raises(SupervisedDenied, match="aliases"):
        scene["collector"].collect(scene["plan"])


def test_shared_mac_disjoint_directory(scene, tmp_path):
    other = tmp_path.resolve() / "other"
    other.mkdir(mode=0o700)
    scene["node"]["mounts"][0].update(
        Type="bind", Source="/host_mnt" + str(other), Destination="/state/data"
    )
    scene["hook"][0] = lambda a, n: (
        "1 0 0:1 / /state/data rw - virtiofs shared rw\n"
        if a[-1] == "/proc/self/mountinfo"
        else None
    )
    assert scene["collector"].collect(scene["plan"])["backing"] == dict(
        kind="mac-shared", source=str(other)
    )


@pytest.mark.parametrize("race", ["local", "metadata", "mount", "inode", "container", "expiry"])
def test_races_fail_closed(scene, race):
    seen = {}

    def hook(argv, count):
        key = (
            argv[-1]
            if argv[0] == "docker" and argv[3] == "exec" and argv[5] == "cat"
            else (argv[5] if argv[0] == "docker" and argv[3] == "exec" else argv[3])
        )
        seen[key] = seen.get(key, 0) + 1
        if argv[0] == "docker" and argv[3] == "inspect" and seen[key] == 1:
            if race == "local":
                path = scene["root"] / "custody/backup"
                path.rename(path.with_name("old"))
                path.mkdir(mode=0o700)
            if race == "metadata":
                scene["resources"]["pvc"]["metadata"]["resourceVersion"] = "2"
            if race == "expiry":
                scene["collector"].clock = lambda: 170
        if race == "mount" and key == "/proc/self/mountinfo" and seen[key] == 2:
            return "2 0 8:1 / /state rw - ext4 /dev/vda rw\n"
        if race == "inode" and key == "stat" and seen[key] == 2:
            return "8:4:41c0:1\n8:2:41c0:1\n8:3:41c0:1\n"
        if race == "container" and argv[3] == "inspect" and seen[key] == 2:
            scene["node"]["pid"] = 2
        return None

    scene["hook"][0] = hook
    with pytest.raises(SupervisedDenied):
        scene["collector"].collect(scene["plan"])
    assert not (scene["root"] / "topology.json").exists()


@pytest.mark.parametrize("output", ["", "x" * (topology.LIMIT + 1), "partial"])
def test_partial_remote_output(scene, output):
    scene["hook"][0] = lambda a, n: output
    with pytest.raises((SupervisedDenied, ValueError)):
        scene["collector"].collect(scene["plan"])
    assert len(scene["calls"]) == 1
    assert not (scene["root"] / "topology.json").exists()


def test_native_denial_no_retry(scene):
    def denied(*args, **kwargs):
        raise PermissionError("synthetic native denial")

    scene["hook"][0] = denied
    with pytest.raises(PermissionError):
        scene["collector"].collect(scene["plan"])
    assert len(scene["calls"]) == 1


def test_real_local_symlink_no_remote(scene):
    path = scene["root"] / "custody/backup"
    shutil.rmtree(path)
    path.symlink_to(scene["root"] / "custody/validate", target_is_directory=True)
    with pytest.raises(OSError):
        scene["collector"].collect(scene["plan"])
    assert not scene["calls"]


def test_expired_plan_no_remote(scene):
    plan = copy.deepcopy(scene["plan"])
    plan["deadline"] = 100
    with pytest.raises(SupervisedDenied, match="expired"):
        scene["collector"].collect(plan)
    assert not scene["calls"]


def test_subpath_mount_denied(scene):
    spec = scene["resources"]["pods"]["items"][0]["spec"]
    spec["containers"][0]["volumeMounts"][0]["subPath"] = "alias"
    with pytest.raises(SupervisedDenied, match="subpath"):
        scene["collector"].collect(scene["plan"])


def test_unrelated_injected_volume_does_not_retarget_state(scene):
    spec = scene["resources"]["pods"]["items"][0]["spec"]
    spec["volumes"] = spec["volumes"] + [dict(name="injected", projected={})]
    assert scene["collector"].collect(scene["plan"])["topology"]["pod"]["state_mounts"]
