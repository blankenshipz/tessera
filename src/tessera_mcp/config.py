"""Configuration contains credential references, never credential values."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    endpoint: str
    token_file: Path
    project_id: str
    destination: str
    ca_file: Path | None = None

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Control endpoint must be an http(s) origin without credentials")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote control endpoints require verified HTTPS")
        if not self.project_id or not self.destination or len(self.destination) > 256:
            raise ValueError("Project and enrolled destination are required")
        if any(c in self.destination for c in "\r\n"):
            raise ValueError("Invalid destination")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                os.environ["TESSERA_CONTROL_URL"].rstrip("/"),
                Path(os.environ["TESSERA_AGENT_TOKEN_FILE"]),
                os.environ["TESSERA_PROJECT_ID"],
                os.environ["TESSERA_AGENT_DESTINATION"],
                Path(os.environ["TESSERA_CONTROL_CA_FILE"])
                if os.environ.get("TESSERA_CONTROL_CA_FILE")
                else None,
            )
        except KeyError as exc:
            raise ValueError("Missing Tessera MCP configuration reference") from exc

    def token(self) -> str:
        """Reopen each call so secure atomic credential replacement supports renewal."""
        try:
            fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 8192
                ):
                    raise ValueError(
                        "Agent credential reference must be an owner-only regular file"
                    )
                token = stream.read(8193).strip()
            if not token or len(token) > 8192 or any(c.isspace() for c in token):
                raise ValueError("Invalid agent credential reference")
            return token
        except OSError:
            raise ValueError("Agent credential reference is unavailable") from None
