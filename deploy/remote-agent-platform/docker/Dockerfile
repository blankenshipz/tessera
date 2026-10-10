FROM python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 HOME=/tmp
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in ./
COPY src ./src
COPY skills/tessera-research-bench ./skills/tessera-research-bench
COPY skills/tessera-governed-research ./skills/tessera-governed-research
RUN uv sync --locked --no-dev --no-editable --extra mcp --extra a2a && groupadd --gid 65532 tessera && useradd --uid 65532 --gid 65532 --no-create-home tessera
USER 65532:65532
CMD ["tessera", "--help"]
