# Dependency provenance and license inventory

`pyproject.toml` is the direct dependency declaration; `uv.lock` pins the resolved
Python runtime/development environment with source URLs and artifact hashes.
`desktop/package-lock.json` separately pins Electron and its dependencies.
The Python build backend is setuptools 77–81 (77+ is required for PEP 639 MIT
license metadata). The Dockerfile additionally pins Python and uv image digests;
those system-image dependencies are not represented by the Python lockfile.

Do not infer third-party license obligations from Tessera's MIT license or a
dependency's name. Produce an inventory from the actual installed distribution:

```sh
uv tree --locked --no-dev --extra mcp
python dev/check_distribution.py /absolute/path/to/tessera_control-0.1.0-py3-none-any.whl
cd desktop && npm ls --all
```

The distribution check's `installed_packages` records exact versions and
`declared_license_metadata` records each installed package's `License-Expression`,
license classifiers and whether legacy license text exists. Missing or ambiguous
metadata remains missing, not an inferred approval. This inventory is a factual
starting point, not a legal review, vulnerability scan, full SBOM or compliance
certificate. Build tools included in a fresh venv appear in the observed inventory
and must not be mislabeled as application runtime dependencies.

The optional `mcp` extra pins the official Python SDK to **1.26.0**. The normal
control/worker install does not require this extra; CI's development image and
distribution checks install it so adapter tests do not silently skip. SDK 1.26.0
uses session-based protocol 2025-11-25; protocol 2026-07-28 is not implemented.
On Intel macOS only, the extra additionally constrains cryptography to
`>=48.0.1,<49`: versions 49/50 no longer publish Intel/universal macOS wheels,
while 48.0.1 publishes a CPython 3.11 universal2 wheel. This avoids silently
bootstrapping a Rust toolchain during the supported package install. Other
platforms retain normal resolution. Distribution checks require binary registry
dependencies; the explicitly supplied Tessera sdist is still built and tested.

Before distribution, retain license/notice files for the exact dependencies and
review bundled Electron/Chromium notices and Docker base-image/system packages
separately. Lockfile hash checks establish artifact identity, not trustworthiness.
Never commit private registry credentials, runtime state or enrollment receipts.
