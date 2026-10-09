# Observed public linux/amd64 manifest 2026-10-07, python:3.12-slim-bookworm.
FROM python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app PYTHONUNBUFFERED=1
# Build-time only. No workload download or inference. Existing typed verifier deps.
COPY examples/tabular-starter/requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /build/requirements.lock
WORKDIR /app
COPY src/tessera_starter /app/tessera_starter
COPY src/tessera_control/dataset_snapshots.py src/tessera_control/dataset_snapshot_models.py src/tessera_control/workspace.py /app/tessera_control/
COPY src/tessera_worker/inputs.py src/tessera_worker/dataset_inputs.py /app/tessera_worker/
COPY examples/tabular-starter/portable_verifier.py /build/portable_verifier.py
# Workload intentionally does not import the control API or ship service credentials.
RUN python /build/portable_verifier.py && touch /app/tessera_control/__init__.py /app/tessera_worker/__init__.py && chmod -R a+rX /app
USER 65532:65532
ENTRYPOINT ["python", "-m", "tessera_starter"]
