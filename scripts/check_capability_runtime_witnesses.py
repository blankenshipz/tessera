"""Opt-in independent runtime witness conformance; disabled without --witness-live."""

from tessera_worker.capability_collector_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
