"""Private durable project-artifact placement, separate from worker output slots.

The filesystem backend receives already quarantined bytes from the control plane;
workers never supply a filesystem path or storage credential.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProjectArtifactStorageError(ValueError):
    """A project-artifact backend cannot safely publish or reconcile bytes."""


@dataclass(frozen=True)
class ProjectArtifactStage:
    tenant_id: str
    project_id: str
    artifact_id: str
    sha256: str
    size: int
    token: str


@dataclass(frozen=True)
class ProjectArtifactPlacement:
    tenant_namespace: str
    project_namespace: str
    object_key: str
    sha256: str
    size: int


class ProjectArtifactBackend(Protocol):
    """Narrow stage/commit/reconcile contract with no URLs or credentials."""

    kind: str

    def stage(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, content: bytes
    ) -> ProjectArtifactStage: ...

    def commit(self, stage: ProjectArtifactStage) -> ProjectArtifactPlacement: ...

    def reconcile(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None: ...

    def inspect(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None: ...

    def delete(
        self, tenant_id: str, project_id: str, artifact_id: str, placement: ProjectArtifactPlacement
    ) -> bool: ...


def verify_project_artifact_placement(
    backend: ProjectArtifactBackend,
    tenant_id: str,
    project_id: str,
    artifact_id: str,
    placement: ProjectArtifactPlacement,
) -> None:
    """Read and checksum a committed object against its authoritative receipt."""
    # Integrity admission is read-only. Publication reconciliation may repair
    # backend state and therefore belongs outside an authority transaction.
    observed = backend.inspect(tenant_id, project_id, artifact_id, placement.sha256, placement.size)
    if observed != placement:
        raise ProjectArtifactStorageError("Project artifact placement is missing or conflicts.")


def _valid(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 200 and "\x00" not in value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FilesystemProjectArtifactBackend:
    """A local private backend using deterministic hashed tenant/project namespaces.

    It only writes content bytes supplied by the control plane.  Publication uses
    a temporary regular file plus ``link`` (not replace), so an existing object is
    never overwritten.  Namespace/object metadata is deliberately relative and
    contains neither an ambient filesystem path nor a remote URL.
    """

    kind = "filesystem-local-v1"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if self.root.exists():
            self._directory(self.root)
        else:
            parent = self.root.parent
            self._directory(parent)
            self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        self._directory(self.root)
        self._root_identity = self._identity(self.root)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        info = path.lstat()
        return info.st_dev, info.st_ino

    @staticmethod
    def _directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise ProjectArtifactStorageError("Project artifact root is unavailable.") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProjectArtifactStorageError("Project artifact root is unsafe.")

    def _root(self) -> None:
        self._directory(self.root)
        if self._identity(self.root) != self._root_identity:
            raise ProjectArtifactStorageError("Project artifact root was replaced.")

    def _names(self, tenant_id: str, project_id: str, artifact_id: str) -> tuple[str, str, str]:
        if not all(_valid(value) for value in (tenant_id, project_id, artifact_id)):
            raise ProjectArtifactStorageError("Project artifact identity is invalid.")
        return (
            _digest("tenant:" + tenant_id),
            _digest("project:" + project_id),
            _digest("artifact:" + artifact_id),
        )

    def _namespace(self, tenant: str, project: str) -> Path:
        self._root()
        current = self.root
        for name in (tenant, project):
            child = current / name
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._directory(child)
            current = child
        return current

    def _existing_namespace(self, tenant: str, project: str) -> Path | None:
        """Resolve an existing namespace without creating or repairing anything."""
        self._root()
        current = self.root
        for name in (tenant, project):
            child = current / name
            try:
                info = child.lstat()
            except FileNotFoundError:
                return None
            except OSError as error:
                raise ProjectArtifactStorageError(
                    "Project artifact namespace is unavailable."
                ) from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ProjectArtifactStorageError("Project artifact namespace is unsafe.")
            current = child
        return current

    @staticmethod
    def _check(sha256: str, content: bytes) -> None:
        if (
            not isinstance(content, bytes)
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or hashlib.sha256(content).hexdigest() != sha256
        ):
            raise ProjectArtifactStorageError("Project artifact checksum does not match content.")

    @staticmethod
    def _read(path: Path, sha256: str, size: int) -> bool:
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != size
            ):
                return False
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                    return False
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                return digest.hexdigest() == sha256
            finally:
                os.close(descriptor)
        except OSError:
            return False

    @staticmethod
    def _recover_published_stage(namespace: Path, target: Path) -> bool:
        """Finish a link publication interrupted before its stage unlink.

        ``link`` makes publication visible before the stage name can be removed.
        A process crash in that gap leaves two links to the same valid inode.  Do
        not treat that recoverable state as corruption, and never remove a stage
        belonging to a different object or an active publisher.
        """
        try:
            target_info = target.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode):
            return False
        recovered = False
        for candidate in namespace.iterdir():
            if not candidate.name.startswith(".stage-"):
                continue
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISLNK(info.st_mode)
                and stat.S_ISREG(info.st_mode)
                and (info.st_dev, info.st_ino) == (target_info.st_dev, target_info.st_ino)
            ):
                candidate.unlink()
                recovered = True
        return recovered

    def stage(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, content: bytes
    ) -> ProjectArtifactStage:
        self._check(sha256, content)
        tenant, project, object_key = self._names(tenant_id, project_id, artifact_id)
        namespace = self._namespace(tenant, project)
        target = namespace / object_key
        self._recover_published_stage(namespace, target)
        if target.exists() or target.is_symlink():
            if not self._read(target, sha256, len(content)):
                raise ProjectArtifactStorageError(
                    "Project artifact identity conflicts with content."
                )
            return ProjectArtifactStage(
                tenant_id, project_id, artifact_id, sha256, len(content), "committed"
            )
        temporary = namespace / (".stage-" + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            written = 0
            while written < len(content):
                count = os.write(descriptor, content[written:])
                if count <= 0:
                    raise OSError("short artifact write")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ProjectArtifactStage(
            tenant_id, project_id, artifact_id, sha256, len(content), temporary.name
        )

    def commit(self, stage: ProjectArtifactStage) -> ProjectArtifactPlacement:
        tenant, project, object_key = self._names(
            stage.tenant_id, stage.project_id, stage.artifact_id
        )
        namespace = self._namespace(tenant, project)
        target = namespace / object_key
        self._recover_published_stage(namespace, target)
        if stage.token != "committed":
            temporary = namespace / stage.token
            if (
                "/" in stage.token
                or not stage.token.startswith(".stage-")
                or not self._read(temporary, stage.sha256, stage.size)
            ):
                raise ProjectArtifactStorageError(
                    "Project artifact stage is unavailable or corrupt."
                )
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        if not self._read(target, stage.sha256, stage.size):
            raise ProjectArtifactStorageError(
                "Project artifact publication conflicts or is corrupt."
            )
        descriptor = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ProjectArtifactPlacement(tenant, project, object_key, stage.sha256, stage.size)

    def reconcile(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None:
        if type(size) is not int or size < 0:
            raise ProjectArtifactStorageError("Project artifact size is invalid.")
        tenant, project, object_key = self._names(tenant_id, project_id, artifact_id)
        namespace = self._namespace(tenant, project)
        target = namespace / object_key
        recovered = self._recover_published_stage(namespace, target)
        if not target.exists() and not target.is_symlink():
            return None
        if not self._read(target, sha256, size):
            raise ProjectArtifactStorageError("Project artifact placement is corrupt or conflicts.")
        if recovered:
            descriptor = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return ProjectArtifactPlacement(tenant, project, object_key, sha256, size)

    def inspect(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None:
        if type(size) is not int or size < 0:
            raise ProjectArtifactStorageError("Project artifact size is invalid.")
        tenant, project, object_key = self._names(tenant_id, project_id, artifact_id)
        namespace = self._existing_namespace(tenant, project)
        if namespace is None:
            return None
        target = namespace / object_key
        if not target.exists() and not target.is_symlink():
            return None
        if not self._read(target, sha256, size):
            raise ProjectArtifactStorageError("Project artifact placement is corrupt or conflicts.")
        return ProjectArtifactPlacement(tenant, project, object_key, sha256, size)

    def delete(
        self, tenant_id: str, project_id: str, artifact_id: str, placement: ProjectArtifactPlacement
    ) -> bool:
        """Remove only the exact committed object; absence is replay-safe.

        The caller must have persisted deletion intent already.  Re-inspection is
        deliberately part of deletion, rather than trusting a stale receipt.
        """
        expected = self.inspect(
            tenant_id, project_id, artifact_id, placement.sha256, placement.size
        )
        if expected is None:
            return False
        if expected != placement:
            raise ProjectArtifactStorageError("Project artifact deletion identity conflicts.")
        # Resolve the known private namespace directly after root verification.
        self._root()
        namespace = self.root / placement.tenant_namespace / placement.project_namespace
        self._directory(namespace)
        target = namespace / placement.object_key
        if not self._read(target, placement.sha256, placement.size):
            raise ProjectArtifactStorageError("Project artifact deletion identity conflicts.")
        target.unlink()
        descriptor = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True


class FakeProjectArtifactBackend:
    """Injectable deterministic backend with one-shot stage/commit fault seams."""

    kind = "fake-project-artifact-v1"

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}
        self.staged: dict[str, tuple[str, str, str, str, bytes]] = {}
        self.fail_stage = False
        self.fail_commit = False

    def stage(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, content: bytes
    ) -> ProjectArtifactStage:
        if self.fail_stage:
            self.fail_stage = False
            raise ProjectArtifactStorageError("Injected project artifact stage failure.")
        FilesystemProjectArtifactBackend._check(sha256, content)
        key = (tenant_id, project_id, artifact_id)
        if key in self.objects:
            if self.objects[key] != content:
                raise ProjectArtifactStorageError(
                    "Project artifact identity conflicts with content."
                )
            return ProjectArtifactStage(
                tenant_id, project_id, artifact_id, sha256, len(content), "committed"
            )
        token = uuid.uuid4().hex
        self.staged[token] = (tenant_id, project_id, artifact_id, sha256, content)
        return ProjectArtifactStage(tenant_id, project_id, artifact_id, sha256, len(content), token)

    def commit(self, stage: ProjectArtifactStage) -> ProjectArtifactPlacement:
        if self.fail_commit:
            self.fail_commit = False
            raise ProjectArtifactStorageError("Injected project artifact commit failure.")
        if stage.token != "committed":
            try:
                tenant, project, artifact, sha256, content = self.staged.pop(stage.token)
            except KeyError as error:
                raise ProjectArtifactStorageError(
                    "Project artifact stage is unavailable."
                ) from error
            if (tenant, project, artifact, sha256, len(content)) != (
                stage.tenant_id,
                stage.project_id,
                stage.artifact_id,
                stage.sha256,
                stage.size,
            ):
                raise ProjectArtifactStorageError("Project artifact stage conflicts.")
            key = (tenant, project, artifact)
            if key in self.objects and self.objects[key] != content:
                raise ProjectArtifactStorageError(
                    "Project artifact identity conflicts with content."
                )
            self.objects[key] = content
        return self.reconcile(
            stage.tenant_id, stage.project_id, stage.artifact_id, stage.sha256, stage.size
        )  # type: ignore[return-value]

    def reconcile(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None:
        content = self.objects.get((tenant_id, project_id, artifact_id))
        if content is None:
            return None
        FilesystemProjectArtifactBackend._check(sha256, content)
        if len(content) != size:
            raise ProjectArtifactStorageError("Project artifact placement conflicts.")
        return ProjectArtifactPlacement(
            _digest("tenant:" + tenant_id),
            _digest("project:" + project_id),
            _digest("artifact:" + artifact_id),
            sha256,
            size,
        )

    def inspect(
        self, tenant_id: str, project_id: str, artifact_id: str, sha256: str, size: int
    ) -> ProjectArtifactPlacement | None:
        return self.reconcile(tenant_id, project_id, artifact_id, sha256, size)

    def delete(
        self, tenant_id: str, project_id: str, artifact_id: str, placement: ProjectArtifactPlacement
    ) -> bool:
        observed = self.inspect(
            tenant_id, project_id, artifact_id, placement.sha256, placement.size
        )
        if observed is None:
            return False
        if observed != placement:
            raise ProjectArtifactStorageError("Project artifact deletion identity conflicts.")
        del self.objects[(tenant_id, project_id, artifact_id)]
        return True
