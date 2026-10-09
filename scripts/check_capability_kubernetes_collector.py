"""Explicit API-only conformance collector; disabled unless --collect-live is supplied."""

from tessera_worker.capability_collector_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
