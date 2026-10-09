"""Partial identity measurements for a supervisor's own live child only.

No PID discovery, command line/environment reads, credential lookup, launches or
complete admission. The caller approves nonsecret artifact references separately.
"""

import ctypes
import hashlib
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from pathlib import Path


class IdentityBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    kind: str
    relative_path: str
    sha256: str


def _process_metadata(process):
    if not isinstance(process, subprocess.Popen) or process.poll() is not None:
        raise IdentityBlocked("owned_child_not_live")
    # Only this owned handle's parent/start metadata; never args or environment.
    reply = subprocess.run(
        ["/bin/ps", "-p", str(process.pid), "-o", "ppid=", "-o", "lstart="],
        capture_output=True,
        timeout=2,
        check=False,
        env={"LC_ALL": "C"},
    )
    fields = reply.stdout.decode("ascii", errors="strict").strip().split(maxsplit=1)
    if reply.returncode or len(fields) != 2 or fields[0] != str(os.getpid()):
        raise IdentityBlocked("owned_child_parent_or_start_unknown")
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_pidpath
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        if function(process.pid, buffer, len(buffer)) <= 0:
            raise IdentityBlocked("owned_child_executable_unknown")
        executable = os.fsdecode(buffer.value)
    elif sys.platform.startswith("linux"):
        executable = os.readlink(f"/proc/{process.pid}/exe")
    else:
        raise IdentityBlocked("owned_child_platform_unsupported")
    if process.poll() is not None:
        raise IdentityBlocked("owned_child_exited_during_measurement")
    return fields[1], executable


class OwnedResearchIdentity:
    """Same-supervisor private receipt, bound to a Popen handle, never a bare PID.

    root and all referenced files must be canonical, caller-owned, non-writable by
    others and nonsymlink. An executable reference must match the OS path readback.
    Receipt/handle cannot be restored from a caller-provided JSON identity claim.
    """

    def __init__(self, process, *, artifact_root, artifacts):
        if not isinstance(process, subprocess.Popen):
            raise IdentityBlocked("owned_child_not_live")
        self._process = process
        self._pid = process.pid if isinstance(process, subprocess.Popen) else None
        self._owner = os.getpid()
        self._receipt = uuid.uuid4().hex
        self._nonces = set()
        self._root = Path(artifact_root)
        if (
            not self._root.is_absolute()
            or self._root != self._root.resolve()
            or not artifacts
            or len(artifacts) > 8
        ):
            raise IdentityBlocked("approved_artifact_root_required")
        self._artifacts = tuple(artifacts)
        ids, kinds = set(), []
        for ref in self._artifacts:
            if (
                not isinstance(ref, ArtifactReference)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", ref.artifact_id)
                or ref.artifact_id in ids
                or ref.kind not in {"executable", "build", "sdk-metadata"}
                or not re.fullmatch(r"[a-f0-9]{64}", ref.sha256)
                or Path(ref.relative_path).is_absolute()
                or any(part in {"", ".", ".."} for part in ref.relative_path.split("/"))
            ):
                raise IdentityBlocked("approved_nonsecret_artifact_reference_required")
            parts = Path(ref.relative_path).parts
            forbidden_suffixes = {
                ".json",
                ".toml",
                ".yaml",
                ".yml",
                ".ini",
                ".conf",
                ".pem",
                ".key",
                ".py",
                ".ts",
                ".js",
                ".md",
                ".c",
            }
            if Path(ref.relative_path).suffix.lower() in forbidden_suffixes or any(
                any(
                    marker in part.lower()
                    for marker in (".env", "credential", "oauth", "secret", "password", "token")
                )
                for part in parts
            ):
                raise IdentityBlocked("source_config_or_secret_reference_denied")
            ids.add(ref.artifact_id)
            kinds.append(ref.kind)
        if kinds.count("executable") != 1:
            raise IdentityBlocked("single_owned_executable_reference_required")
        self._start, executable = self._metadata()
        ref = next(r for r in self._artifacts if r.kind == "executable")
        if executable != str(self._root / ref.relative_path):
            raise IdentityBlocked("owned_executable_reference_mismatch")
        self._measure_artifacts()

    def _metadata(self):
        if os.getpid() != self._owner or self._process.pid != self._pid:
            raise IdentityBlocked("supervisor_or_process_handle_changed")
        return _process_metadata(self._process)

    @staticmethod
    def _safe_stat(fd, *, directory=False):
        value = os.fstat(fd)
        valid = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
        if (
            not valid
            or value.st_uid != os.getuid()
            or value.st_mode & 0o022
            or (not directory and value.st_nlink != 1)
        ):
            raise IdentityBlocked("artifact_ownership_or_type_denied")
        return value

    def _measure_artifacts(self):
        results = []
        for ref in self._artifacts:
            descriptors = []
            try:
                fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(fd)
                self._safe_stat(fd, directory=True)
                parts = ref.relative_path.split("/")
                for part in parts[:-1]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(fd)
                    self._safe_stat(fd, directory=True)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                descriptors.append(fd)
                before = self._safe_stat(fd)
                limit = 65536 if ref.kind == "sdk-metadata" else 64 * 1024 * 1024
                if not 1 <= before.st_size <= limit:
                    raise IdentityBlocked("artifact_size_bound")
                digest, size, metadata = hashlib.sha256(), 0, bytearray()
                while chunk := os.read(fd, 65536):
                    size += len(chunk)
                    if size > limit:
                        raise IdentityBlocked("artifact_size_bound")
                    digest.update(chunk)
                    if ref.kind == "sdk-metadata":
                        metadata.extend(chunk)
                after = self._safe_stat(fd)

                def stable(s):
                    return (
                        s.st_dev,
                        s.st_ino,
                        s.st_size,
                        s.st_mtime_ns,
                        s.st_ctime_ns,
                        s.st_mode,
                        s.st_uid,
                        s.st_gid,
                        s.st_nlink,
                    )

                if (
                    stable(before) != stable(after)
                    or size != before.st_size
                    or digest.hexdigest() != ref.sha256
                ):
                    raise IdentityBlocked("approved_artifact_changed")
                item = {
                    "artifact_id": ref.artifact_id,
                    "kind": ref.kind,
                    "sha256": digest.hexdigest(),
                    "size": size,
                }
                if ref.kind == "sdk-metadata":
                    # Standard installed dist-info metadata, not config/version assertions.
                    if (
                        parts[-1] != "METADATA"
                        or len(parts) < 2
                        or not parts[-2].endswith(".dist-info")
                    ):
                        raise IdentityBlocked("installed_sdk_metadata_required")
                    parsed = BytesParser().parsebytes(bytes(metadata), headersonly=True)
                    name, version = parsed.get_all("Name", []), parsed.get_all("Version", [])
                    if (
                        len(name) != 1
                        or name[0] not in {"mcp", "opencode-ai", "opencode"}
                        or len(version) != 1
                        or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", version[0])
                    ):
                        raise IdentityBlocked("installed_sdk_metadata_unknown")
                    item.update(package=name[0], installed_version=version[0])
                results.append(item)
            except OSError as error:
                raise IdentityBlocked("approved_artifact_access_denied") from error
            finally:
                for fd in reversed(descriptors):
                    os.close(fd)
        return results

    def observe(self, nonce):
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
            raise IdentityBlocked("bounded_fresh_nonce_required")
        if nonce in self._nonces or len(self._nonces) >= 4096:
            raise IdentityBlocked("nonce_reused_or_observation_bound")
        self._nonces.add(nonce)
        start, executable = self._metadata()
        ref = next(r for r in self._artifacts if r.kind == "executable")
        if start != self._start or executable != str(self._root / ref.relative_path):
            raise IdentityBlocked("owned_process_start_or_executable_changed")
        artifacts = self._measure_artifacts()
        if self._metadata() != (start, executable):
            raise IdentityBlocked("owned_process_changed_during_measurement")
        now = datetime.now(UTC)
        return {
            "protocol": "tessera-owned-research-identity-v1",
            "complete": False,
            "nonce": nonce,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=30)).isoformat(),
            "process": {
                "pid": self._pid,
                "parent_pid": self._owner,
                "started_at": start,
                "supervisor_receipt_id": self._receipt,
                "live": True,
            },
            "artifacts": artifacts,
            "missing": [
                "effective_config",
                "final_dispatch_hook_order",
                "included_oauth_entitlement",
                "included_billing_policy",
                "launched_mcp_sdk_identity",
                "harness_build_binding",
            ],
            "scope": "owned_child_executable_and_approved_artifact_bytes_only",
        }
