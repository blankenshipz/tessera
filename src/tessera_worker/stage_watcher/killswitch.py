"""Fail-closed operator kill switch for the stage watcher (ADR 0085 reader, ADR 0087 section 7).

Marker PRESENT means engaged. The only clear state is: the marker path is absolute, `lstat`
reports it absent, and its parent is a directory. Everything else (no path configured, the
marker present, a dangling symlink, a missing or non-directory parent, any other OS error)
reads as engaged. The marker is re-read on every call; nothing is cached.

In this slice the switch gates only future model calls and requests. It never gates observe or
report, and it is never a cancel: an engaged switch does not stop or interrupt a run.
"""

from __future__ import annotations

import os
import stat


class KillSwitch:
    def __init__(self, path: str | os.PathLike[str] | None):
        self.path = None if path is None else os.fspath(path)

    def engaged(self) -> bool:
        path = self.path
        if path is None or not isinstance(path, str) or not os.path.isabs(path):
            return True
        try:
            os.lstat(path)
            return True
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return True
        try:
            parent = os.stat(os.path.dirname(path))
        except (OSError, ValueError):
            return True
        return not stat.S_ISDIR(parent.st_mode)

    def may_call_model(self) -> bool:
        return not self.engaged()

    def may_request(self) -> bool:
        return not self.engaged()

    @staticmethod
    def may_observe() -> bool:
        """Observe is never gated by the switch (ADR 0087 section 7.1)."""
        return True

    @staticmethod
    def may_report() -> bool:
        """Report is never gated by the switch (ADR 0087 section 7.1)."""
        return True
