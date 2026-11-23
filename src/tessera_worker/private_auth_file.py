"""The one private-file check for the operator's registry auth file.

The worker's startup check and the CPU runtime's mount-time check both call this, so they
cannot drift: ``lstat`` (never follow a link), a regular file, owned by the worker user, a single
link, and no group or world permission bit. The file is never read, repaired or reported."""

from __future__ import annotations

import os
import stat
from pathlib import Path

CODES = (
    "auth_file_unavailable",
    "auth_file_symlink",
    "auth_file_not_regular",
    "auth_file_wrong_owner",
    "auth_file_hardlinked",
    "auth_file_group_or_world_access",
)


class AuthFileRefused(ValueError):  # noqa: N818 - a refusal carrying a closed code
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_private_auth_file(path: Path | str) -> Path:
    """Return the path when it is private, else raise ``AuthFileRefused`` with a closed code."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise AuthFileRefused("auth_file_unavailable") from None
    if stat.S_ISLNK(info.st_mode):
        raise AuthFileRefused("auth_file_symlink")
    if not stat.S_ISREG(info.st_mode):
        raise AuthFileRefused("auth_file_not_regular")
    if info.st_uid != os.geteuid():
        raise AuthFileRefused("auth_file_wrong_owner")
    if info.st_nlink != 1:
        raise AuthFileRefused("auth_file_hardlinked")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise AuthFileRefused("auth_file_group_or_world_access")
    return path
