FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
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
