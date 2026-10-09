#!/usr/bin/env python3
"""Opt-in wrapper for the installed pre-Job preparation evidence CLI.

Requires an exclusive stopped worker. Does not start services, submit Jobs,
clean helpers, post control mutations, or change attempt outcomes. The distinct
pre_job_preparation proof must never be relabelled as a zero-create attestation.
"""

from tessera_worker.kubernetes_preparation_recovery import main

if __name__ == "__main__":
    main()
