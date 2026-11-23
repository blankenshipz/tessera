"""Read a code review's source from the platform's own git remote (ADR 0108).

The operator configures repository names that map to local bare repositories on the control
host (:class:`GitRemotes`, loaded by :func:`load_git_remotes`). A client names a repository, a
branch, the head commit it expects and a base branch; :meth:`GitRemotes.resolve` checks the
branch still points at that commit, finds the merge base with the base branch and lists the
changed paths. :meth:`GitRemotes.files_at` and :meth:`GitRemotes.tree_has_files` read regular
files at a commit.

Client-supplied names reach git only after validation: repository names come from the operator
mapping, branch names pass a strict pattern and ``git check-ref-format --branch`` and are always
used as explicit ``refs/heads/<name>`` refs, commits are 40 lowercase hex digits and paths are
passed after ``--`` with literal pathspecs. Git runs without a shell, with a minimal
environment, no global or system configuration, a timeout and capped output; only local
read-only plumbing is used (never fetch, clone or any remote command).

Refusals are :class:`GitSourceRefused` with a stable ``code``; messages never carry git's
stderr, repository paths or file paths.

Secret and data guard: before any content reaches a snapshot (and so a model destination), a
changed path matching a secret-shaped name, a secret or data directory or an operator ``deny_paths``
glob refuses the whole submit ``path_denied``, and a file whose content has a private-key PEM
header or an API-key shaped token refuses it ``content_denied``. The refusal carries only the
matched rule's label (``GitSourceRefused.rule``), never a path or any content.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_CHANGED_FILES = 400
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PATHS = 512
MAX_PATH_BYTES = 4096
GIT_TIMEOUT_SECONDS = 30.0
# Listing output (diff-tree, ls-tree): generous for MAX_PATHS long paths, far below memory risk.
MAX_LISTING_BYTES = 4 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024

REFUSAL_CODES = (
    "invalid_repo",
    "repo_not_configured",
    "invalid_branch",
    "invalid_base_branch",
    "invalid_head_sha",
    "invalid_sha",
    "invalid_path",
    "branch_not_found",
    "head_mismatch",
    "base_not_found",
    "commit_not_found",
    "no_changes",
    "change_too_large",
    "unsupported_entry",
    "path_denied",
    "content_denied",
    "git_failed",
)

# Built-in secret/data guard. Names match a path's last segment, directories any earlier segment;
# all case-insensitive. An operator ``allow_paths`` glob exempts a path from these built-in
# rules (never from ``deny_paths`` or the content check).
_DENY_NAME_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "*.pfx",
    "credentials*",
    "secrets*",
    "*.sqlite*",
    "*.db",
)
_DENY_DIRS = frozenset({".aws", ".ssh", ".gnupg", "data", "datasets"})
# Narrow, high-signal content patterns: a PEM private-key header and well-known API token shapes.
_SECRET_CONTENT = (
    ("private_key_pem", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("anthropic_api_key", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key_id", re.compile(rb"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}")),
)
MAX_GUARD_GLOBS = 64

_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REGULAR_MODES = frozenset({"100644", "100755"})
_UNSUPPORTED_MODES = frozenset({"120000", "160000"})
_ZERO_SHA = "0" * 40
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
# Defensive per-invocation configuration; a bare repository's own config still applies, so
# only plumbing commands are used and external diff/textconv are disabled explicitly.
_CONFIG_ARGS = (
    "-c",
    "protocol.allow=never",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


class GitSourceRefused(Exception):
    """A git source request was refused; ``code`` is one of :data:`REFUSAL_CODES`."""

    def __init__(self, code: str, rule: str | None = None):
        if code not in REFUSAL_CODES:
            raise ValueError("unknown git source refusal code")
        super().__init__(code)
        self.code = code
        self.rule = rule  # the matched guard rule's label; never a path or content


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # "A", "M" or "D"


@dataclass(frozen=True)
class Resolution:
    repo: str
    branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    changed: tuple[ChangedFile, ...]

    def changed_dicts(self) -> list[dict[str, str]]:
        return [{"path": item.path, "status": item.status} for item in self.changed]


@dataclass(frozen=True)
class _Entry:
    mode: str
    kind: str
    oid: str
    size: int | None


class _OutputTooLarge(Exception):
    pass


def valid_repo_name(name: object) -> bool:
    return isinstance(name, str) and _REPO_RE.fullmatch(name) is not None


def _valid_branch_shape(name: object) -> bool:
    return (
        isinstance(name, str)
        and _BRANCH_RE.fullmatch(name) is not None
        and ".." not in name
        and "@{" not in name
        and not name.startswith("-")
    )


def _check_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None or value == _ZERO_SHA:
        raise GitSourceRefused(code)
    return value


def _check_paths(paths: Iterable[str]) -> list[str]:
    if isinstance(paths, str):
        raise GitSourceRefused("invalid_path")
    result: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path or "\0" in path:
            raise GitSourceRefused("invalid_path")
        if len(path.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
            raise GitSourceRefused("invalid_path")
        if path.startswith("/") or path.endswith("/"):
            raise GitSourceRefused("invalid_path")
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise GitSourceRefused("invalid_path")
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise GitSourceRefused("invalid_path") from None
        result.append(path)
    if len(result) > MAX_PATHS:
        raise GitSourceRefused("change_too_large")
    return list(dict.fromkeys(result))


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH") or _DEFAULT_PATH,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
    }


def _check_globs(globs: Iterable[str]) -> tuple[str, ...]:
    if isinstance(globs, str):
        raise ValueError("path globs must be a list of strings")
    result: list[str] = []
    for glob in globs:
        if (
            not isinstance(glob, str)
            or not glob
            or len(glob) > 200
            or "\0" in glob
            or glob.startswith("/")
            or ".." in glob.split("/")
        ):
            raise ValueError("path globs must be non-empty relative glob strings")
        result.append(glob.lower())
    if len(result) > MAX_GUARD_GLOBS:
        raise ValueError("too many path globs")
    return tuple(dict.fromkeys(result))


def _glob_match(path: str, glob: str) -> bool:
    """A glob without ``/`` matches any path segment; one with ``/`` is anchored at the
    repository root and matches the path or any directory prefix of it. ``*`` stays in a segment."""
    segments = path.split("/")
    if "/" not in glob:
        return any(fnmatch.fnmatchcase(segment, glob) for segment in segments)
    parts = glob.rstrip("/").split("/")
    return len(segments) >= len(parts) and all(
        fnmatch.fnmatchcase(s, g) for s, g in zip(segments, parts, strict=False)
    )


class GitRemotes:
    """Operator mapping of repository name → local bare repository path on the control host."""

    def __init__(
        self,
        mapping: Mapping[str, Path | str],
        *,
        max_changed_files: int = MAX_CHANGED_FILES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        timeout_seconds: float = GIT_TIMEOUT_SECONDS,
        deny_paths: Iterable[str] = (),
        allow_paths: Iterable[str] = (),
    ):
        self._deny_paths = _check_globs(deny_paths)
        self._allow_paths = _check_globs(allow_paths)
        repos: dict[str, Path] = {}
        for name, raw in mapping.items():
            if not valid_repo_name(name):
                raise ValueError("git remote names must match the repository name pattern")
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError("git remote paths must be absolute")
            repos[name] = path
        if max_changed_files < 1 or max_total_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("git source limits must be positive")
        self._repos = repos
        self.max_changed_files = max_changed_files
        self.max_total_bytes = max_total_bytes
        self.timeout_seconds = timeout_seconds
        env = _git_env()
        self._env = env
        self._git = shutil.which("git", path=env["PATH"])

    @property
    def repo_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._repos))

    # -- public API -------------------------------------------------------------------------

    def resolve(
        self, repo: str, branch: str, head_sha: str, base_branch: str = "main"
    ) -> Resolution:
        path = self._repo_path(repo)
        branch = self._check_branch(path, branch, "invalid_branch")
        base_branch = self._check_branch(path, base_branch, "invalid_base_branch")
        head_sha = _check_sha(head_sha, "invalid_head_sha")

        tip = self._branch_tip(path, branch)
        if tip is None:
            raise GitSourceRefused("branch_not_found")
        if tip != head_sha:
            raise GitSourceRefused("head_mismatch")
        if self._object_type(path, head_sha) != "commit":
            raise GitSourceRefused("head_mismatch")
        base_tip = self._branch_tip(path, base_branch)
        if base_tip is None or self._object_type(path, base_tip) != "commit":
            raise GitSourceRefused("base_not_found")
        code, out = self._run(path, ["merge-base", head_sha, base_tip], max_output=128)
        if code == 1:
            raise GitSourceRefused("base_not_found")  # no common ancestor
        if code != 0:
            raise GitSourceRefused("git_failed")
        base_sha = out.decode("ascii", "replace").strip()
        if _SHA_RE.fullmatch(base_sha) is None:
            raise GitSourceRefused("git_failed")
        if base_sha == head_sha:
            raise GitSourceRefused("no_changes")

        changed = self._diff(path, base_sha, head_sha)
        if not changed:
            raise GitSourceRefused("no_changes")
        if len(changed) > self.max_changed_files:
            raise GitSourceRefused("change_too_large")
        self.check_paths_allowed(item.path for item in changed)

        # Size budget over base + head contents; also refuses symlinks/submodules early.
        base_paths = [c.path for c in changed if c.status in ("M", "D")]
        head_paths = [c.path for c in changed if c.status in ("A", "M")]
        total = 0
        for sha, paths in ((base_sha, base_paths), (head_sha, head_paths)):
            entries = self._entries(path, sha, paths)
            for item in paths:
                entry = entries.get(item)
                if entry is None:
                    raise GitSourceRefused("git_failed")  # diff and tree disagree
                total += self._regular_size(entry)
                if total > self.max_total_bytes:
                    raise GitSourceRefused("change_too_large")
        return Resolution(repo, branch, head_sha, base_branch, base_sha, tuple(changed))

    def files_at(self, repo: str, sha: str, paths: Iterable[str]) -> dict[str, bytes]:
        """Bytes of each path that exists at ``sha``; absent paths are skipped.

        A symlink, submodule or directory at a requested path is refused ``unsupported_entry``.
        """
        path = self._repo_path(repo)
        sha = _check_sha(sha, "invalid_sha")
        wanted = _check_paths(paths)
        self.check_paths_allowed(wanted)
        self._require_commit(path, sha)
        if not wanted:
            return {}
        entries = self._entries(path, sha, wanted)
        present: list[tuple[str, _Entry]] = []
        total = 0
        for item in wanted:
            entry = entries.get(item)
            if entry is None:
                continue
            total += self._regular_size(entry)
            if total > self.max_total_bytes:
                raise GitSourceRefused("change_too_large")
            present.append((item, entry))
        if not present:
            return {}
        blobs = self._read_blobs(path, present, total)
        self.check_content_allowed(blobs.values())
        return blobs

    def tree_has_files(self, repo: str, sha: str, paths: Iterable[str]) -> set[str]:
        """Which of ``paths`` are regular files (not directories, symlinks or submodules)."""
        path = self._repo_path(repo)
        sha = _check_sha(sha, "invalid_sha")
        wanted = _check_paths(paths)
        self._require_commit(path, sha)
        if not wanted:
            return set()
        entries = self._entries(path, sha, wanted)
        return {
            item
            for item in wanted
            if (entry := entries.get(item)) is not None
            and entry.kind == "blob"
            and entry.mode in _REGULAR_MODES
        }

    def check_paths_allowed(self, paths: Iterable[str]) -> None:
        """Refuse ``path_denied`` if any path matches the secret/data guard (see module doc)."""
        for item in paths:
            rule = self._denied_rule(item)
            if rule is not None:
                raise GitSourceRefused("path_denied", rule)

    def check_content_allowed(self, contents: Iterable[bytes]) -> None:
        """Refuse ``content_denied`` if any content has a private key or API token shape."""
        for data in contents:
            for label, pattern in _SECRET_CONTENT:
                if pattern.search(data) is not None:
                    raise GitSourceRefused("content_denied", label)

    def _denied_rule(self, item: str) -> str | None:
        lowered = item.lower()
        for glob in self._deny_paths:
            if _glob_match(lowered, glob):
                return "operator_deny_paths"
        if any(_glob_match(lowered, glob) for glob in self._allow_paths):
            return None
        *directories, name = lowered.split("/")
        for directory in directories:
            if directory in _DENY_DIRS:
                return "directory:" + directory
        for glob in _DENY_NAME_GLOBS:
            if fnmatch.fnmatchcase(name, glob):
                return "name:" + glob
        return None

    # -- validation -------------------------------------------------------------------------

    def _repo_path(self, repo: object) -> Path:
        if not valid_repo_name(repo):
            raise GitSourceRefused("invalid_repo")
        path = self._repos.get(repo)  # type: ignore[arg-type]
        if path is None:
            raise GitSourceRefused("repo_not_configured")
        return path

    def _check_branch(self, path: Path, name: object, code: str) -> str:
        if not _valid_branch_shape(name):
            raise GitSourceRefused(code)
        assert isinstance(name, str)
        rc, out = self._run(path, ["check-ref-format", "--branch", name], max_output=1024)
        if rc != 0 or out.decode("utf-8", "replace").strip() != name:
            raise GitSourceRefused(code)
        return name

    # -- git plumbing -----------------------------------------------------------------------

    def _branch_tip(self, path: Path, branch: str) -> str | None:
        ref = "refs/heads/" + branch
        rc, out = self._run(path, ["show-ref", "--verify", "--", ref], max_output=1024)
        if rc != 0:
            return None
        line = out.decode("utf-8", "replace").strip()
        parts = line.split(" ", 1)
        if len(parts) != 2 or parts[1] != ref or _SHA_RE.fullmatch(parts[0]) is None:
            return None
        return parts[0]

    def _object_type(self, path: Path, sha: str) -> str | None:
        rc, out = self._run(path, ["cat-file", "-t", sha], max_output=64)
        if rc != 0:
            return None
        return out.decode("ascii", "replace").strip()

    def _require_commit(self, path: Path, sha: str) -> None:
        if self._object_type(path, sha) != "commit":
            raise GitSourceRefused("commit_not_found")

    def _diff(self, path: Path, base_sha: str, head_sha: str) -> list[ChangedFile]:
        args = [
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--name-status",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            head_sha,
            "--",
        ]
        rc, out = self._run(path, args, max_output=MAX_LISTING_BYTES, too_large="change_too_large")
        if rc != 0:
            raise GitSourceRefused("git_failed")
        fields = out.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise GitSourceRefused("git_failed")
        changed: list[ChangedFile] = []
        seen: set[str] = set()
        for index in range(0, len(fields), 2):
            status = fields[index].decode("ascii", "replace")
            item = self._decode_path(fields[index + 1])
            if status == "T":
                status = "M"  # type change; the entry check refuses symlinks/submodules
            if status not in ("A", "M", "D") or item in seen:
                raise GitSourceRefused("unsupported_entry")
            seen.add(item)
            changed.append(ChangedFile(item, status))
            if len(changed) > self.max_changed_files:
                raise GitSourceRefused("change_too_large")
        return changed

    @staticmethod
    def _decode_path(raw: bytes) -> str:
        try:
            item = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitSourceRefused("unsupported_entry") from None
        try:
            _check_paths([item])
        except GitSourceRefused:
            raise GitSourceRefused("unsupported_entry") from None
        return item

    def _entries(self, path: Path, sha: str, paths: list[str]) -> dict[str, _Entry]:
        if not paths:
            return {}
        args = ["ls-tree", "-z", "-l", "--full-tree", sha, "--", *paths]
        rc, out = self._run(path, args, max_output=MAX_LISTING_BYTES, too_large="change_too_large")
        if rc != 0:
            raise GitSourceRefused("git_failed")
        wanted = set(paths)
        entries: dict[str, _Entry] = {}
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, sep, raw_path = record.partition(b"\t")
            if not sep:
                raise GitSourceRefused("git_failed")
            try:
                item = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if item not in wanted:
                continue
            parts = meta.decode("ascii", "replace").split()
            if len(parts) != 4 or _SHA_RE.fullmatch(parts[2]) is None:
                raise GitSourceRefused("git_failed")
            mode, kind, oid, size_text = parts
            size = int(size_text) if size_text.isdigit() else None
            entries[item] = _Entry(mode, kind, oid, size)
        return entries

    @staticmethod
    def _regular_size(entry: _Entry) -> int:
        if entry.mode in _UNSUPPORTED_MODES or entry.kind != "blob":
            raise GitSourceRefused("unsupported_entry")
        if entry.mode not in _REGULAR_MODES or entry.size is None:
            raise GitSourceRefused("unsupported_entry")
        return entry.size

    def _read_blobs(
        self, path: Path, present: list[tuple[str, _Entry]], total: int
    ) -> dict[str, bytes]:
        request = b"".join(entry.oid.encode("ascii") + b"\n" for _, entry in present)
        # Header per object: "<oid> blob <size>\n" plus a trailing newline after the content.
        cap = total + len(present) * 80
        rc, out = self._run(
            path,
            ["cat-file", "--batch"],
            stdin=request,
            max_output=cap,
            too_large="change_too_large",
        )
        if rc != 0:
            raise GitSourceRefused("git_failed")
        result: dict[str, bytes] = {}
        offset = 0
        for item, entry in present:
            newline = out.find(b"\n", offset)
            if newline < 0:
                raise GitSourceRefused("git_failed")
            header = out[offset:newline].decode("ascii", "replace").split(" ")
            if len(header) != 3 or header[0] != entry.oid or header[1] != "blob":
                raise GitSourceRefused("git_failed")
            if not header[2].isdigit() or int(header[2]) != entry.size:
                raise GitSourceRefused("git_failed")
            start = newline + 1
            end = start + entry.size
            if out[end : end + 1] != b"\n":
                raise GitSourceRefused("git_failed")
            result[item] = out[start:end]
            offset = end + 1
        if offset != len(out):
            raise GitSourceRefused("git_failed")
        return result

    def _run(
        self,
        path: Path,
        args: list[str],
        *,
        max_output: int,
        stdin: bytes | None = None,
        too_large: str = "git_failed",
    ) -> tuple[int, bytes]:
        if self._git is None:
            raise GitSourceRefused("git_failed")
        command = [self._git, "--git-dir", str(path), *_CONFIG_ARGS, *args]
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
                cwd=str(path) if path.is_dir() else "/",
                close_fds=True,
                shell=False,
            )
        except OSError:
            raise GitSourceRefused("git_failed") from None
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(self.timeout_seconds, _expire)
        timer.daemon = True
        timer.start()
        writer: threading.Thread | None = None
        if stdin is not None:
            assert proc.stdin is not None
            pipe = proc.stdin

            def _write() -> None:
                try:
                    pipe.write(stdin)
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    try:
                        pipe.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=_write, daemon=True)
            writer.start()
        chunks: list[bytes] = []
        size = 0
        too_big = False
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_output:
                    too_big = True
                    proc.kill()
                    break
                chunks.append(chunk)
        finally:
            proc.stdout.close()
            try:
                proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            timer.cancel()
            if writer is not None:
                writer.join(timeout=1)
        if timed_out.is_set():
            raise GitSourceRefused("git_failed")
        if too_big:
            raise GitSourceRefused(too_large)
        return proc.returncode, b"".join(chunks)


def load_git_remotes(path: Path | str, **limits: object) -> GitRemotes:
    """Load ``{"repos": {"name": "/abs/path.git"}}`` plus optional ``deny_paths`` and
    ``allow_paths`` glob lists (see the module doc); raises ValueError on a malformed file."""
    with open(path, "rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("git remotes config is too large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("git remotes config is not valid JSON") from None
    optional = {"deny_paths", "allow_paths"}
    if (
        not isinstance(data, dict)
        or "repos" not in data
        or not set(data) <= {"repos"} | optional
        or not isinstance(data["repos"], dict)
    ):
        raise ValueError('git remotes config must be {"repos": {name: path}} plus optional lists')
    for key in optional & set(data):
        if not isinstance(data[key], list):
            raise ValueError(f"git remotes config {key} must be a list of globs")
        limits.setdefault(key, data[key])
    mapping: dict[str, Path] = {}
    for name, value in data["repos"].items():
        if not isinstance(value, str):
            raise ValueError("git remote paths must be strings")
        mapping[name] = Path(value)
    return GitRemotes(mapping, **limits)  # type: ignore[arg-type]
