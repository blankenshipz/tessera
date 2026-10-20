"""Explicit administrator-owned private credential backend; no ambient discovery.

The directory must already exist, belong to this server uid and have mode 0700.
Enrollment is a server-only Python API, not a control/API/CLI operation. A locked,
atomic record contains metadata and bytes together, preventing version TOCTOU.
Python/HTTP libraries cannot promise whole-process erasure or prevent uid-owner
access; this backend is for an isolated trusted server, never a desktop/workload.
"""

import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager

from .salad_secrets import SecretBuffer, SecretMetadata


class PrivateFileSecretResolver:
    def __init__(self, directory, *, now=time.time):
        self._directory = os.fspath(directory)
        self._now = now
        # Open each component without following symlinks, including ancestors.
        path = os.path.abspath(self._directory)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.split("/")[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._private(os.fstat(fd), directory=True)
            self._directory_fd = os.dup(fd)
        finally:
            os.close(fd)

    def __repr__(self):
        return "<PrivateFileSecretResolver private>"

    def close(self):
        os.close(self._directory_fd)

    @staticmethod
    def _private(info, *, directory=False):
        if (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
        ):
            raise ValueError("private credential backend unavailable")

    @staticmethod
    def _name(reference):
        if type(reference) is not str or not re.fullmatch(
            r"credential-ref:[a-z0-9-]{1,63}", reference
        ):
            raise ValueError("credential unavailable")
        return reference.split(":")[1] + ".credential"

    @contextmanager
    def _locked(self):
        directory = os.dup(self._directory_fd)
        lock = None
        try:
            self._private(os.fstat(directory), directory=True)
            lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            self._private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        raw = bytearray()
        try:
            self._private(os.fstat(fd))
            with os.fdopen(fd, "rb", closefd=False) as stream:
                header = stream.readline(1025)
                if not header.endswith(b"\n") or len(header) > 1024:
                    raise ValueError("credential unavailable")
                metadata = json.loads(header)
                if set(metadata) != {"version", "expires_at", "revoked", "purpose"}:
                    raise ValueError("credential unavailable")
                if (
                    type(metadata["version"]) is not int
                    or not 1 <= metadata["version"] <= 2**31 - 1
                    or type(metadata["revoked"]) is not bool
                    or metadata["purpose"] != "salad_management"
                    or type(metadata["expires_at"]) not in {int, float}
                    or not math.isfinite(metadata["expires_at"])
                ):
                    raise ValueError("credential unavailable")
                size = os.fstat(fd).st_size - len(header)
                if not 1 <= size <= 4096:
                    raise ValueError("credential unavailable")
                raw = bytearray(size)
                if stream.readinto(raw) != len(raw):
                    raise ValueError("credential unavailable")
            return metadata, SecretBuffer(raw)
        except Exception:
            for i in range(len(raw)):
                raw[i] = 0
            raise ValueError("credential unavailable") from None
        finally:
            os.close(fd)

    def metadata(self, reference):
        with self._locked() as directory:
            metadata, secret = self._read(directory, self._name(reference))
            secret.zeroize()
            return SecretMetadata(checked_at=self._now(), **metadata)

    def resolve(self, reference, version):
        with self._locked() as directory:
            metadata, secret = self._read(directory, self._name(reference))
            if (
                metadata["version"] != version
                or metadata["revoked"]
                or metadata["expires_at"] <= self._now()
            ):
                secret.zeroize()
                raise ValueError("credential unavailable")
            return secret

    def enroll(self, reference, version, secret, *, expires_at):
        """Explicit administrator rotation; consumes/wipes the supplied owned buffer."""
        if type(secret) is not SecretBuffer:
            raise ValueError("owned buffer required")
        with secret:
            if (
                type(version) is not int
                or not 1 <= version <= 2**31 - 1
                or type(expires_at) not in {int, float}
                or not math.isfinite(expires_at)
                or expires_at <= self._now()
                or not 1 <= len(secret._buffer) <= 4096
                or b"\r" in secret._buffer
                or b"\n" in secret._buffer
            ):
                raise ValueError("credential enrollment rejected")
            with self._locked() as directory:
                name = self._name(reference)
                try:
                    previous, old = self._read(directory, name)
                except FileNotFoundError:
                    previous = None
                else:
                    old.zeroize()
                if previous and version <= previous["version"]:
                    raise ValueError("credential version must increase")
                self._write(
                    directory,
                    name,
                    dict(
                        version=version,
                        expires_at=expires_at,
                        revoked=False,
                        purpose="salad_management",
                    ),
                    secret,
                )

    def renew(self, reference, *, expected_version, expires_at):
        """Explicit trusted-admin same-key renewal, not an access-lease refresh.

        Current human authority must be established by the calling administrator.
        No API/MCP route exposes this method. Expired references may be renewed;
        revoked references may not. CAS prevents concurrent or duplicate renewal.
        """
        now = self._now()
        if (
            type(expected_version) is not int
            or not 1 <= expected_version < 2**31 - 1
            or type(expires_at) not in {int, float}
            or not math.isfinite(expires_at)
            or not now < expires_at <= now + 24 * 60 * 60
        ):
            raise ValueError("credential renewal rejected")
        with self._locked() as directory:
            name = self._name(reference)
            metadata, secret = self._read(directory, name)
            with secret:
                if (
                    metadata["version"] != expected_version
                    or metadata["revoked"]
                    or expires_at <= metadata["expires_at"]
                ):
                    raise ValueError("credential renewal rejected")
                renewed = metadata | {"version": expected_version + 1, "expires_at": expires_at}
                self._write(directory, name, renewed, secret)
                return SecretMetadata(checked_at=now, **renewed)

    def revoke(self, reference):
        with self._locked() as directory:
            name = self._name(reference)
            metadata, secret = self._read(directory, name)
            with secret:
                self._write(directory, name, metadata | {"revoked": True}, secret)

    @staticmethod
    def _write(directory, name, metadata, secret):
        temporary = ".enroll-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(json.dumps(metadata, allow_nan=False).encode() + b"\n")
                stream.write(secret._buffer)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
