"""Four fixed immutable release records, NOT a future-capacity reservation.

No truncate/preallocation/free-space claim: exact prepared/claim/unknown bytes
are materialized and fsynced BEFORE the quota effect. Ack data is staged/fsynced
before create-only publication, retaining the staging inode (no cleanup). Visible
ack bytes on reconnect are only observed/unconfirmed, including after directory
fsync uncertainty; no filesystem marker certifies prior syscall completion.
This is NOT guaranteed ack storage under exhaustion. No expiry release exists.
"""

import json
import os
import stat
from pathlib import Path

from .bundle_export import directory_identity
from .maintenance_witness import read_json
from .schema_supervised import SupervisedDenied

LIMIT = 262144
SUFFIXES = {"prepared", "claim", "failure", "ack"}


class ReleaseJournal:
    def __init__(self, directory, identity):
        self.directory, self.identity = Path(directory), identity

    def check(self):
        if directory_identity(self.directory) != self.identity:
            raise SupervisedDenied("release journal identity drift")
        if self.directory.stat().st_uid != os.getuid():
            raise SupervisedDenied("release journal owner changed")

    def write(self, suffix, value):
        if suffix not in SUFFIXES:
            raise SupervisedDenied("unknown release record")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > LIMIT:
            raise SupervisedDenied("release record exceeds bound")
        self.check()
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if (info.st_dev, info.st_ino) != (self.identity["device"], self.identity["inode"]):
                raise SupervisedDenied("release journal raced")
            name = "release." + suffix + ".json"
            staged = ".release.ack.staged"
            fd = os.open(
                staged if suffix == "ack" else name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if suffix == "ack":
                # Keep the staged inode as immutable evidence, even on failure.
                # link is create-only, unlike rename which could replace a record.
                self.check()
                os.link(
                    staged, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
                )
            os.fsync(directory)
            self.check()
            if self.read(suffix) != value:
                raise SupervisedDenied("release journal readback mismatch")
        finally:
            os.close(directory)

    def read(self, suffix):
        if suffix not in SUFFIXES:
            raise SupervisedDenied("unknown release record")
        self.check()
        path = self.directory / ("release." + suffix + ".json")
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != (2 if suffix == "ack" else 1)
        ):
            raise SupervisedDenied("release record ownership/alias changed")
        if suffix == "ack":
            staged = (self.directory / ".release.ack.staged").lstat()
            if not stat.S_ISREG(staged.st_mode) or (staged.st_dev, staged.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise SupervisedDenied("release ack staging identity changed")
        if suffix == "ack":
            # The generic evidence reader correctly forbids all hardlinks. Only
            # this fixed ack format permits its exact retained staging link.
            def identity(s):
                return (
                    s.st_dev,
                    s.st_ino,
                    s.st_mode,
                    s.st_uid,
                    s.st_nlink,
                    s.st_size,
                    s.st_mtime_ns,
                    s.st_ctime_ns,
                )

            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                before = os.fstat(stream.fileno())
                if identity(before) != identity(info) or before.st_size > LIMIT:
                    raise SupervisedDenied("release ack changed or exceeds bound")
                raw = stream.read(LIMIT + 1)
                after = os.fstat(stream.fileno())
            if (
                len(raw) > LIMIT
                or identity(before) != identity(after)
                or identity(after) != identity(path.lstat())
                or identity(after) != identity((self.directory / ".release.ack.staged").lstat())
            ):
                raise SupervisedDenied("release ack changed during read")
            value = json.loads(raw)
        else:
            value = read_json(path)[0]
        self.check()
        return value
