# The Kubernetes lab profile explicitly selects this hardened target. It has no
# root runtime user; the Pod supplies a writable, fsGroup-owned /state volume.
FROM python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS lab
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in ./
COPY src ./src
COPY skills ./skills
RUN uv sync --locked --no-dev && chmod -R a+rX /app/src && groupadd --gid 65532 tessera && useradd --uid 65532 --gid 65532 --no-create-home tessera
USER 65532:65532
CMD ["tessera", "serve", "--state-dir", "/state/control", "--port", "8765", "--bind-host", "0.0.0.0"]

# The final/default Docker target remains the development/test image used by
# Compose. Compose also names it explicitly so later stage changes cannot drift.
FROM python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS dev
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Only development/tests need compiler, patch and ps for owned identity fixtures.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev patch procps && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in ./
COPY src ./src
COPY skills ./skills
RUN uv sync --locked --extra mcp
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "dev/control.py"]
