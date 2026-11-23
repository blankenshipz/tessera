"""A close-time write failure in ``_copy_private`` is a destination failure.

It is not an unreadable source.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tessera_control import deploy_upgrade


def test_close_time_error_is_a_write_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "src.bin"
    source.write_bytes(b"x" * 100)
    real_fdopen = os.fdopen

    class FailingClose:
        def __init__(self, handle):
            self._handle = handle

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def close(self):
            self._handle.close()
            raise OSError(28, "no space left")

    monkeypatch.setattr(
        deploy_upgrade.os, "fdopen", lambda fd, mode: FailingClose(real_fdopen(fd, mode))
    )
    with pytest.raises(deploy_upgrade._CopyWriteError):
        deploy_upgrade._copy_private(source, tmp_path / "dst.bin")


def test_normal_copy_and_source_error(tmp_path: Path) -> None:
    source = tmp_path / "src.bin"
    source.write_bytes(b"abc")
    digest = deploy_upgrade._copy_private(source, tmp_path / "dst.bin")
    assert (tmp_path / "dst.bin").read_bytes() == b"abc" and len(digest) == 64
    with pytest.raises(OSError) as raised:
        deploy_upgrade._copy_private(tmp_path / "missing", tmp_path / "dst2.bin")
    assert not isinstance(raised.value, deploy_upgrade._CopyWriteError)
