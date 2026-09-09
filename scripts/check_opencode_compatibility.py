#!/usr/bin/env python3
"""Run the explicitly configured, provider-disabled real OpenCode fixture."""

from tessera_worker.opencode_compatibility_fixture import main  # noqa: E402

raise SystemExit(main())
