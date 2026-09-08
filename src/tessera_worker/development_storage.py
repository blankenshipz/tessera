"""Explicit development-only retention of synthetic stopped-workspace outputs.

This boundary is intentionally separate from :mod:`tessera_worker.runtime`.
It only adapts a marked synthetic fixture to ``SealedOutputJournal`` and the
existing quarantine upload protocol; it does not enable runtime storage or
execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .artifacts import ArtifactChannel
from .sealed_outputs import SealedOutputJournal

DEVELOPMENT_STORAGE_DIRECTORY = "development-only-artifact-seals"
DEVELOPMENT_STORAGE_LABEL = "development-local-quarantine"


class DevelopmentStorageError(ValueError):
    """A development-only storage input is not an explicitly synthetic fixture."""


@dataclass(frozen=True)
class SyntheticStoppedWorkspace:
    """A caller-marked fixture workspace that has already stopped."""

    path: Path
    lease_id: str
    epoch: int
    output_paths: tuple[str, ...]
    max_output_bytes: int
    termination_observation: dict[str, Any]
    synthetic: Literal[True] = True
    stopped: Literal[True] = True

    def __post_init__(self) -> None:
        if self.synthetic is not True or self.stopped is not True:
            raise DevelopmentStorageError(
                "Development storage accepts only synthetic stopped workspaces."
            )
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise DevelopmentStorageError("Synthetic workspace must be an absolute Path.")
        if type(self.output_paths) is not tuple:
            raise DevelopmentStorageError("Synthetic output declarations must be a tuple.")


class DevelopmentArtifactStorage:
    """Fixed-path development custody for synthetic outputs only.

    ``root`` is a developer-owned private root; this class creates one named
    child and never changes runtime package admission, mounts, or output flags.
    """

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise DevelopmentStorageError("Development storage root must be an absolute Path.")
        self.label = DEVELOPMENT_STORAGE_LABEL
        self.root = root / DEVELOPMENT_STORAGE_DIRECTORY
        self.journal = SealedOutputJournal(self.root)

    def seal(self, workspace: SyntheticStoppedWorkspace) -> str:
        if not isinstance(workspace, SyntheticStoppedWorkspace):
            raise DevelopmentStorageError("A synthetic stopped workspace is required.")
        return self.journal.seal(
            workspace=workspace.path,
            lease_id=workspace.lease_id,
            expected_epoch=workspace.epoch,
            output_paths=workspace.output_paths,
            max_output_bytes=workspace.max_output_bytes,
            termination_observation=workspace.termination_observation,
        )

    def upload(self, channel: ArtifactChannel, seal_id: str) -> dict[str, Any]:
        """Resume a sealed fixture through the existing quarantine-only API."""

        return self.journal.resume(channel, seal_id)

    def seal_and_upload(
        self, channel: ArtifactChannel, workspace: SyntheticStoppedWorkspace
    ) -> dict[str, Any]:
        return self.upload(channel, self.seal(workspace))


__all__ = [
    "DEVELOPMENT_STORAGE_DIRECTORY",
    "DEVELOPMENT_STORAGE_LABEL",
    "DevelopmentArtifactStorage",
    "DevelopmentStorageError",
    "SyntheticStoppedWorkspace",
]
