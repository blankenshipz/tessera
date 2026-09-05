from __future__ import annotations

import hashlib
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import tessera_worker.outputs as outputs_module
from tessera_worker.outputs import CollectedOutput, OutputCollectionError, collect_outputs


def test_collects_nested_binary_and_empty_files_as_immutable_records(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    payload = b"\x00\xffbinary"
    (tmp_path / "nested" / "data.bin").write_bytes(payload)
    (tmp_path / "empty").write_bytes(b"")

    result = collect_outputs(tmp_path, ["nested/data.bin", "empty"], len(payload))

    assert isinstance(result, tuple)
    assert result == (
        CollectedOutput("nested/data.bin", payload, hashlib.sha256(payload).hexdigest()),
        CollectedOutput("empty", b"", hashlib.sha256(b"").hexdigest()),
    )
    with pytest.raises(AttributeError):
        result[0].relative_path = "changed"  # type: ignore[misc]


def test_enforces_cumulative_limit_before_reading_later_files(tmp_path: Path) -> None:
    (tmp_path / "first").write_bytes(b"123")
    (tmp_path / "second").write_bytes(b"456")

    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["first", "second"], 5)


@pytest.mark.parametrize(
    "paths",
    [
        ["/absolute"],
        ["../escape"],
        ["nested/../escape"],
        ["nested\\escape"],
        ["C:file"],
        ["nested//file"],
        ["nested/."],
        ["a/x", "A/y"],
        ["a", "a/child"],
        ["e\u0301", "é"],
    ],
)
def test_rejects_ambiguous_or_unsafe_declarations(tmp_path: Path, paths: list[str]) -> None:
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, paths, 100)


def test_rejects_symlink_parent_leaf_hardlink_and_special_files(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"data")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "data").write_bytes(b"data")
    (tmp_path / "link-parent").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "link-leaf").symlink_to(target)
    (tmp_path / "hardlink").hardlink_to(target)

    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["link-parent/data"], 100)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["link-leaf"], 100)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["hardlink"], 100)

    fifo = tmp_path / "fifo"
    if hasattr(stat, "S_IFIFO"):
        os.mkfifo(fifo)
        with pytest.raises(OutputCollectionError):
            collect_outputs(tmp_path, ["fifo"], 100)

    sock_path = tmp_path / "socket"
    sock = socket.socket(socket.AF_UNIX)
    try:
        try:
            sock.bind(str(sock_path))
        except OSError:
            pass
        else:
            with pytest.raises(OutputCollectionError):
                collect_outputs(tmp_path, ["socket"], 100)
    finally:
        sock.close()


def test_preflights_fifo_before_final_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    original_open = outputs_module.os.open
    opened: list[object] = []

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(outputs_module.os, "open", recording_open)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["fifo"], 100)
    assert opened == [tmp_path]


def test_replacement_with_fifo_after_preflight_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = tmp_path / "value"
    value.write_bytes(b"value")
    original_open = outputs_module.os.open

    def replace_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "value":
            value.unlink()
            os.mkfifo(value)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(outputs_module.os, "open", replace_before_open)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["value"], 100)


def test_hardlink_added_between_preflight_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = tmp_path / "value"
    hardlink = tmp_path / "hardlink"
    value.write_bytes(b"value")
    original_open = outputs_module.os.open

    def add_hardlink_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "value":
            os.link(value, hardlink)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(outputs_module.os, "open", add_hardlink_before_open)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["value"], 100)
    monkeypatch.undo()
    hardlink.unlink()
    assert collect_outputs(tmp_path, ["value"], 100)[0].content == b"value"


def test_rejects_root_symlink_and_keeps_descriptors_closed_on_error(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "value").write_bytes(b"value")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OutputCollectionError):
        collect_outputs(root_link, ["value"], 100)

    with pytest.raises(OutputCollectionError):
        collect_outputs(target, ["missing"], 100)
    assert collect_outputs(target, ["value"], 100)[0].content == b"value"


def test_rejects_cross_device_file_from_mocked_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = tmp_path / "value"
    value.write_bytes(b"value")
    original_fstat = outputs_module.os.fstat
    calls = 0

    def cross_device(fd: int) -> object:
        nonlocal calls
        calls += 1
        info = original_fstat(fd)
        if calls == 2:
            return SimpleNamespace(
                **{
                    name: getattr(info, name)
                    for name in (
                        "st_mode",
                        "st_ino",
                        "st_nlink",
                        "st_uid",
                        "st_gid",
                        "st_size",
                        "st_atime_ns",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                },
                st_dev=info.st_dev + 1,
            )
        return info

    monkeypatch.setattr(outputs_module.os, "fstat", cross_device)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["value"], 100)


def test_rejects_mocked_metadata_change_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = tmp_path / "value"
    value.write_bytes(b"value")
    original_fstat = outputs_module.os.fstat
    calls = 0

    def changed_after_read(fd: int) -> object:
        nonlocal calls
        calls += 1
        info = original_fstat(fd)
        if calls == 3:
            return SimpleNamespace(
                **{
                    name: getattr(info, name)
                    for name in (
                        "st_mode",
                        "st_ino",
                        "st_dev",
                        "st_nlink",
                        "st_uid",
                        "st_gid",
                        "st_size",
                        "st_atime_ns",
                        "st_mtime_ns",
                    )
                },
                st_ctime_ns=info.st_ctime_ns + 1,
            )
        return info

    monkeypatch.setattr(outputs_module.os, "fstat", changed_after_read)
    with pytest.raises(OutputCollectionError):
        collect_outputs(tmp_path, ["value"], 100)
