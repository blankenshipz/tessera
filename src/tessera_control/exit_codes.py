"""Process exit codes shared by the long-running Tessera services (ADR 0214).

A supervisor restarts a service that exits ``RUNTIME_FAILURE`` (``Restart=on-failure``) and does
not restart one that exits ``CONFIG_REFUSED`` (``RestartPreventExitStatus=2``): a refused operator
file stays refused until a person edits it, so restarting it only loops. Everything that is not a
refusal of the operator's own configuration (a key fetch that timed out at boot, a database that
was busy, an exception escaping a service loop) is a runtime failure, so it is restarted.

``--check-config`` (``tessera serve``, the Salad executor and the Salad guardian) runs the same
startup parsing and validation and exits with these codes too: ``OK`` when startup would go on,
``CONFIG_REFUSED`` when startup would refuse the configuration, ``RUNTIME_FAILURE`` when the check
itself could not run.
"""

from __future__ import annotations

OK = 0
RUNTIME_FAILURE = 1
CONFIG_REFUSED = 2

# The one JSON line a successful ``--check-config`` prints on stdout.
CONFIG_CHECK_SCHEMA = "tessera.config-check.v1"
