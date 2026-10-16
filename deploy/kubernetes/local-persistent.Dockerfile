FROM python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 HOME=/tmp
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in CONTRIBUTING.md DESIGN.md CHANGELOG.md SECURITY.md AGENTS.md ./
COPY docs ./docs
COPY desktop ./desktop
COPY deploy ./deploy
COPY skills ./skills
COPY plugins ./plugins
COPY examples ./examples
COPY workloads ./workloads
COPY release ./release
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && groupadd --gid 65532 tessera && useradd --uid 65532 --gid 65532 --no-create-home tessera
USER 65532:65532
CMD ["tessera", "--help"]
