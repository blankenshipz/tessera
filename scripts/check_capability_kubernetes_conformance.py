"""Explicit offline capability profile/schema; live path is deliberately fail-closed."""

from tessera_worker.capability_conformance import main

if __name__ == "__main__":
    raise SystemExit(main())
