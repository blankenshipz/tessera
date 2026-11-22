"""Process exit codes shared by the worker CLI and the worker daemon.

A supervisor (the worker daemon, systemd, launchd or a script) can tell these apart:

* ``EXIT_OK``: the command finished. A non-terminal ``unknown`` result is still 0.
* ``EXIT_TERMINAL``: the worker produced terminal failure evidence for an operation
  (for example a permanent control refusal). Restarting will not change it; an operator
  has to act. The JSON result on stdout names the operation and reason.
* ``EXIT_CREDENTIAL_REJECTED``: control refused the worker credential. Restarting
  immediately cannot help; the credential must be renewed or replaced. A supervisor
  should back off for a long time and must not crash-loop.

* ``EXIT_REPORT_PENDING``: the run's observation could not be delivered to control and waits in
  the worker's durable outbox (ADR 0054). Restarting the same command with backoff redelivers it
  (the grant is recovered, never started again); a supervisor should retry.

Any other non-zero status is an unexpected failure and is retried with backoff.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_TERMINAL = 2
EXIT_CREDENTIAL_REJECTED = 3
EXIT_REPORT_PENDING = 4
