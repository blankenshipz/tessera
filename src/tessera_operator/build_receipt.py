"""Frozen occupied-target image build, with private durable provenance receipts.

No authority/data mounts, namespace writes, bootstrap, rollout or automatic replay.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from mcp.types import ToolAnnotations

from . import upgrade
from . import upgrade_support as activation
from . import upgrade_support as admission
from . import upgrade_support as target

LOCAL_BUILDER = "desktop-linux"


def _execution_env():
    # Keep normal Desktop context/registry credential-helper references, not
    # credential values. Do not inherit client endpoint, builder, source-policy,
    # proxy/build-argument, Git, Python or loader overrides from the caller.
    return {
        "HOME": "/Users/zach",
        "DOCKER_CONFIG": "/Users/zach/.docker",
        "BUILDX_CONFIG": "/Users/zach/.docker/buildx",
        "DOCKER_CONTEXT": "desktop-linux",
        "DOCKER_BUILDKIT": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
    }


def _query(*args):
    try:
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", *args],
            env=_execution_env(),
            capture_output=True,
            timeout=20,
        )
        if result.returncode:
            raise ValueError()
        return result.stdout.decode().strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise target.AdmissionError("local Docker/builder identity unavailable") from None


def _native_builder_inspect():
    """Decode the supported inspect text, never guess/fall back to a builder.

    Buildx v0.36.1-desktop.1 has no inspect --format flag. Require the observed
    header/core grammar and bounded known metadata sections; an indented device
    Name is not another node. Unknown/duplicate/missing fields fail closed.
    """
    text = _query("buildx", "inspect", LOCAL_BUILDER)
    if len(text.encode()) > 65536 or len(text.splitlines()) > 1024:
        raise ValueError()
    header, node = {}, {}
    in_nodes = False
    metadata = None
    metadata_rows = 0
    rules = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        if line == "Nodes:":
            if in_nodes or set(header) != {"Name", "Driver", "Last Activity"}:
                raise ValueError()
            in_nodes = True
            continue
        if in_nodes and (
            line in {"Labels:", "Devices:"} or re.fullmatch(r"GC Policy rule#[0-9]+:", line)
        ):
            if (
                set(node) != {"Name", "Endpoint", "Status", "BuildKit version", "Platforms"}
                or line in rules
            ):
                raise ValueError()
            if metadata is not None and metadata_rows == 0:
                raise ValueError()
            rules.add(line)
            metadata = line
            metadata_rows = 0
            continue
        if line.startswith(" "):
            if metadata is None or ":" not in line:
                raise ValueError()
            key, value = line.strip().split(":", 1)
            if not key or not value.strip():
                raise ValueError()
            if metadata == "Labels:":
                if not re.fullmatch(r"org\.mobyproject\.buildkit\.worker\.[a-zA-Z0-9_.-]+", key):
                    raise ValueError()
            elif metadata == "Devices:":
                if key not in {"Name", "Automatically allowed"}:
                    raise ValueError()
            elif key not in {
                "All",
                "Filters",
                "Keep Duration",
                "Max Used Space",
                "Reserved Space",
                "Min Free Space",
            }:
                raise ValueError()
            metadata_rows += 1
            continue
        if metadata is not None or ":" not in line:
            raise ValueError()
        key, value = line.split(":", 1)
        fields = node if in_nodes else header
        allowed = (
            {"Name", "Endpoint", "Status", "BuildKit version", "Platforms"}
            if in_nodes
            else {"Name", "Driver", "Last Activity"}
        )
        if key not in allowed or key in fields or not value.strip():
            raise ValueError()
        fields[key] = value.strip()
    if not in_nodes or set(node) != {"Name", "Endpoint", "Status", "BuildKit version", "Platforms"}:
        raise ValueError()
    if metadata is not None and metadata_rows == 0:
        raise ValueError()
    return {
        "Name": header["Name"],
        "Driver": header["Driver"],
        "Nodes": [
            {
                "Name": node["Name"],
                "Endpoint": node["Endpoint"],
                "Status": node["Status"],
                "Buildkit": node["BuildKit version"],
            }
        ],
    }


def _builder_identity():
    try:
        observed = _native_builder_inspect()
        nodes = observed["Nodes"]
        if (
            observed["Name"] != LOCAL_BUILDER
            or observed["Driver"] != "docker"
            or len(nodes) != 1
            or nodes[0]["Endpoint"] != "desktop-linux"
            or nodes[0]["Status"] != "running"
            or nodes[0].get("DriverOpts")
            or nodes[0].get("BuildkitdFlags")
        ):
            raise ValueError()
        node = nodes[0]
        # These selected public fields identify the local embedded builder; do
        # not retain arbitrary builder environment, daemon flags or auth config.
        identity = {
            "name": LOCAL_BUILDER,
            "driver": "docker",
            "endpoint": "desktop-linux",
            "node": node["Name"],
            "buildkit": node["Buildkit"],
        }
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", identity["node"]) or not re.fullmatch(
            r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9_.-]+)?", identity["buildkit"]
        ):
            raise ValueError()
        return identity
    except (KeyError, TypeError, ValueError):
        raise target.AdmissionError("explicit local embedded Docker builder required") from None


def _capacity():
    # Capacity must be measured on the same sanitized Desktop target as build,
    # not via a caller-selected DOCKER_CONFIG/context reference.
    if shutil.disk_usage(upgrade.ROOT.parent).free < 5 * 1024**3:
        raise target.AdmissionError("host receipt reserve insufficient")
    raw = _query("exec", "desktop-control-plane", "df", "-Pk", "/var/lib/containerd")
    if int(raw.splitlines()[-1].split()[3]) * 1024 < upgrade.MIN_NODE_FREE:
        raise target.AdmissionError("node build/import reserve insufficient")


def _runtime():
    base, pod, _ = upgrade._snapshot()
    backend = upgrade._backend(pod)
    endpoint = _query(
        "context", "inspect", "desktop-linux", "--format", "{{json .Endpoints.docker.Host}}"
    )
    if endpoint != '"unix:///Users/zach/.docker/run/docker.sock"':
        raise target.AdmissionError("Docker Desktop endpoint changed")
    return {
        "kubernetes": target._kubeconfig_identity(),
        "docker": endpoint,
        "node_id": _query("inspect", "--format", "{{.Id}}", "desktop-control-plane"),
        "builder": _builder_identity(),
        "execution_policy": "desktop-sanitized-v1",
        **{key: base[key] for key in upgrade.IDENTITY},
        "old_image": base["image"],
        "schema": backend["schema"],
        "incarnation": backend["incarnation"],
        "store_sha256": backend["store_sha256"],
    }


def plan() -> dict:
    inventory = upgrade.candidate_inventory()
    published = subprocess.run(
        ["git", "-C", str(target.REPOSITORY), "rev-parse", "refs/remotes/origin/main"],
        capture_output=True,
        env=_execution_env(),
        timeout=10,
    )
    if published.returncode or published.stdout.decode().strip() != inventory["git_commit"]:
        raise target.AdmissionError("candidate must match locally recorded published main")
    runtime = _runtime()
    if runtime["store_sha256"] != inventory["overlays"]["src/tessera_control/store.py"]:
        raise target.AdmissionError("build fence overlay differs from active store")
    value = {"inventory_sha256": inventory["inventory_sha256"], "runtime": runtime}
    return value | {"build_plan_sha256": upgrade._digest(value)}


def _run(directory, label, argv, *, timeout=60):
    """Retain full private stdout/stderr/status for each bounded command."""
    out = directory / (label + ".stdout")
    err = directory / (label + ".stderr")
    code = None
    failure = None
    try:
        with out.open("xb") as stdout, err.open("xb") as stderr:
            process = subprocess.run(
                argv, stdout=stdout, stderr=stderr, timeout=timeout, env=_execution_env()
            )
            code = process.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = type(error).__name__
    activation._write_once(
        directory / (label + ".json"),
        {
            "argv": argv,
            "exit_code": code,
            "failure": failure,
        },
    )
    if code != 0:
        raise target.AdmissionError(
            "build command failed/uncertain; retained private logs: " + label
        )
    return out


def _docker(directory, label, *args, timeout=60):
    return _run(directory, label, ["docker", "--context", "desktop-linux", *args], timeout=timeout)


def _file_inventory(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise target.AdmissionError("unsafe frozen context entry")
        if path.is_file():
            with path.open("rb") as stream:
                entries[path.relative_to(root).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return entries


def _freeze(directory, inventory):
    context = directory / "context"
    context.mkdir(mode=0o700)
    archive = directory / "source.tar"
    _run(
        directory,
        "source",
        [
            "git",
            "-C",
            str(target.REPOSITORY),
            "archive",
            "--format=tar",
            "--output",
            str(archive),
            inventory["git_commit"],
        ],
    )
    expected = dict(inventory["tracked_blobs"])
    seen = set()
    with tarfile.open(archive) as source:
        for member in source:
            name = member.name.rstrip("/")
            if (
                not name
                or Path(name).is_absolute()
                or any(p in {"..", "."} for p in Path(name).parts)
                or not (member.isdir() or member.isfile())
            ):
                raise target.AdmissionError("unsafe published source archive")
            if member.isdir():
                continue
            if name not in expected or name in seen:
                raise target.AdmissionError("unexpected published source entry")
            data = source.extractfile(member).read()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blob != expected[name]:
                raise target.AdmissionError("published source blob changed")
            path = context / name
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o555 if member.mode & 0o111 else 0o444)
            seen.add(name)
    if seen != set(expected):
        raise target.AdmissionError("published source inventory incomplete")
    for name, digest in inventory["overlays"].items():
        data = (target.REPOSITORY / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise target.AdmissionError("reviewed overlay changed before freeze")
        path = context / name
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(data)
        path.chmod(0o444)
    files = _file_inventory(context)
    activation._write_once(
        directory / "frozen.json",
        {
            "source": inventory,
            "files": files,
            "context_sha256": upgrade._digest(files),
        },
    )
    for path in sorted(context.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o500)
    context.chmod(0o500)
    return context, upgrade._digest(files)


def _unchanged(directory, context, digest, expected):
    if upgrade._digest(_file_inventory(context)) != digest or plan() != expected:
        raise target.AdmissionError("frozen inputs or target changed; do not import/deploy")


def build(operation: str, expected_build_plan_sha256: str) -> dict:
    with upgrade._locked():
        current = plan()
        if current["build_plan_sha256"] != expected_build_plan_sha256:
            raise target.AdmissionError("build plan stale")
        _capacity()  # Before context/archive/build growth on either volume.
        runs = upgrade.ROOT / "builds"
        runs.mkdir(mode=0o700, exist_ok=True)
        directory = runs / upgrade._operation(operation).name
        if directory.exists():
            raise target.AdmissionError(
                "prior build attempt retained; inspect status, never replay"
            )
        directory.mkdir(mode=0o700)
        activation._write_once(directory / "intent.json", current)
        try:
            inventory = upgrade.candidate_inventory()
            if inventory["inventory_sha256"] != current["inventory_sha256"]:
                raise target.AdmissionError("source changed before freeze")
            context, context_digest = _freeze(directory, inventory)
            _unchanged(directory, context, context_digest, current)
            tag = "docker.io/library/tessera-local-persistent:upgrade-" + operation
            _docker(
                directory,
                "build",
                "buildx",
                "build",
                "--builder",
                LOCAL_BUILDER,
                "--load",
                "--platform",
                "linux/amd64",
                "--provenance=false",
                "--sbom=false",
                "--file",
                str(context / "deploy/kubernetes/local-persistent.Dockerfile"),
                "--tag",
                tag,
                str(context),
                timeout=900,
            )
            inspect_path = _docker(directory, "inspect", "image", "inspect", tag)
            image = json.loads(inspect_path.read_text())[0]
            if image["Os"] != "linux" or image["Architecture"] != "amd64":
                raise target.AdmissionError("built image platform mismatch")
            code = """import hashlib,json,pathlib
import tessera_control.store as s
import tessera_operator
files={}
for name,root in [('tessera_control',pathlib.Path(s.__file__).parent),
                  ('tessera_operator',pathlib.Path(tessera_operator.__file__).parent)]:
 for path in sorted(root.rglob('*.py')):
  files['src/'+name+'/'+path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({'store_sha256':hashlib.sha256(pathlib.Path(s.__file__).read_bytes()).hexdigest(),
 'schema':s.SCHEMA_VERSION,'package_sha256':hashlib.sha256(
  json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()}))
"""
            contents = _docker(
                directory,
                "contents",
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65532:65532",
                "--entrypoint=/app/.venv/bin/python",
                image["Id"],
                "-c",
                code,
            )
            observed = json.loads(contents.read_text())
            packaged = {
                name: digest
                for name, digest in _file_inventory(context).items()
                if name.endswith(".py")
                and name.startswith(("src/tessera_control/", "src/tessera_operator/"))
            }
            if observed != {
                "schema": current["runtime"]["schema"],
                "store_sha256": current["runtime"]["store_sha256"],
                "package_sha256": upgrade._digest(packaged),
            }:
                raise target.AdmissionError("built package/schema/store fence mismatch")
            _unchanged(directory, context, context_digest, current)
            _capacity()
            # Save the unique tag: ctr retains it for manifest lookup. The
            # imported config must still match the immutable inspected image ID.
            _docker(
                directory,
                "export-tagged",
                "image",
                "save",
                "--output",
                str(directory / "tagged.tar"),
                tag,
            )
            # Import via stdin without a shared node filesystem or shell pipeline.
            _import(directory, directory / "tagged.tar")
            listing = _docker(
                directory,
                "node-list",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "ls",
            ).read_text()
            rows = [
                line.split()
                for line in listing.splitlines()
                if line.split() and line.split()[0] == tag
            ]
            digests = (
                [v for v in rows[0] if re.fullmatch(r"sha256:[a-f0-9]{64}", v)]
                if len(rows) == 1
                else []
            )
            if len(digests) != 1:
                raise target.AdmissionError("imported image manifest unavailable")
            manifest = _docker(
                directory,
                "manifest",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "content",
                "get",
                digests[0],
            ).read_bytes()
            if "sha256:" + hashlib.sha256(manifest).hexdigest() != digests[0]:
                raise target.AdmissionError("node manifest digest mismatch")
            if json.loads(manifest)["config"]["digest"] != image["Id"]:
                raise target.AdmissionError("imported node image differs from inspected build")
            pinned = "docker.io/library/tessera-local-persistent@" + digests[0]
            _docker(
                directory,
                "pin",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "tag",
                tag,
                pinned,
            )
            pins = (
                _docker(
                    directory,
                    "pins",
                    "exec",
                    "desktop-control-plane",
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "ls",
                )
                .read_text()
                .splitlines()
            )
            final_rows = [
                line.split() for line in pins if line.split() and line.split()[0] == pinned
            ]
            final_digests = (
                [v for v in final_rows[0] if re.fullmatch(r"sha256:[a-f0-9]{64}", v)]
                if len(final_rows) == 1
                else []
            )
            if final_digests != digests:
                raise target.AdmissionError(
                    "final pin descriptor digest differs from inspected image"
                )
            final_manifest = _docker(
                directory,
                "pinned-manifest",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "content",
                "get",
                final_digests[0],
            ).read_bytes()
            if (
                "sha256:" + hashlib.sha256(final_manifest).hexdigest() != digests[0]
                or json.loads(final_manifest).get("config", {}).get("digest") != image["Id"]
            ):
                raise target.AdmissionError("final pin identity differs from inspected build")
            _unchanged(directory, context, context_digest, current)
            receipt = {
                "image": pinned,
                "git_commit": inventory["git_commit"],
                "inventory_sha256": inventory["inventory_sha256"],
                "schema": observed["schema"],
                "operation": operation,
                "context_sha256": context_digest,
                "runtime": current["runtime"],
                "package_sha256": observed["package_sha256"],
                "image_config": image["Id"],
                "producer": "frozen-build-v1",
            }
            activation._write_once(directory / "verified.json", receipt)
            candidates = upgrade.ROOT / "candidates"
            candidates.mkdir(mode=0o700, exist_ok=True)
            activation._write_once(candidates / (digests[0].split(":")[1] + ".json"), receipt)
            activation._write_once(
                directory / "result.json", {"state": "verified", "image": pinned}
            )
            return {
                "state": "verified_image_only_no_rollout",
                "image": pinned,
                "operation": operation,
            }
        except Exception:
            # Retain complete command evidence; never embed stderr, SQL or credentials.
            if not (directory / "result.json").exists():
                activation._write_once(
                    directory / "result.json", {"state": "failed_or_uncertain_no_replay"}
                )
            raise


def _import(directory, archive):
    argv = [
        "docker",
        "--context",
        "desktop-linux",
        "exec",
        "-i",
        "desktop-control-plane",
        "ctr",
        "--namespace",
        "k8s.io",
        "images",
        "import",
        "-",
    ]
    code = None
    failure = None
    try:
        with (
            archive.open("rb") as source,
            (directory / "import.stdout").open("xb") as out,
            (directory / "import.stderr").open("xb") as err,
        ):
            code = subprocess.run(
                argv, stdin=source, stdout=out, stderr=err, timeout=180, env=_execution_env()
            ).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = type(error).__name__
    activation._write_once(
        directory / "import.json", {"argv": argv, "exit_code": code, "failure": failure}
    )
    if code != 0:
        raise target.AdmissionError("image import failed/uncertain; logs retained, no replay")


def status(operation: str) -> dict:
    directory = upgrade.ROOT / "builds" / upgrade._operation(operation).name
    admission._receipt(directory / "intent.json")
    if (directory / "result.json").exists():
        return admission._receipt(directory / "result.json")
    return {"state": "interrupted_or_running_unknown_no_replay", "operation": operation}


def verify_receipt(receipt, inventory):
    if receipt.get("producer") != "frozen-build-v1":
        raise target.AdmissionError("trusted image build provenance required")
    directory = upgrade.ROOT / "builds" / upgrade._operation(receipt["operation"]).name
    if (
        admission._receipt(directory / "verified.json") != receipt
        or receipt["runtime"] != _runtime()
    ):
        raise target.AdmissionError("build provenance or target runtime changed")
    if receipt["inventory_sha256"] != inventory["inventory_sha256"]:
        raise target.AdmissionError("build provenance source changed")


def register(server):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    server.tool(name="local_control_build_plan", annotations=read)(plan)
    server.tool(name="local_control_build", annotations=write)(build)
    server.tool(name="local_control_build_status", annotations=read)(status)
