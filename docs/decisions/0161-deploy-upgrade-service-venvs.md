# 0161 — Deploy upgrade refreshes registered service venvs

## Status

Proposed. Extends [ADR 0099](0099-deploy-upgrade.md). No schema change and no migration. It adds no
authority: it runs `pip install` with an interpreter the operator names in the plan, without a shell.

## Context

`tessera deploy-upgrade` restarted services but only the control plane's own environment held the new
release. A service that runs from its own virtualenv (the reasoning service installs one) kept the old
code after an upgrade, so a "successful" deploy could restart a stale service.

## Decision

1. A plan service may declare `venv` (absolute directory). The plan also names `install_source`
   (default `repo_root`); a venv without either is a plan error.
2. In the existing order, after stop, backup, migrate and config fixes and before any restart, the
   release is installed into each distinct venv with `<venv>/bin/python -m pip install <source>`, then
   the venv's interpreter reports the installed version, which must equal the control plane's when
   that is known.
3. The receipt gets `service_venvs` (venv, services, release, version, ok, error_class) and one
   `install-venv:<path>` step per venv. The first failure stops the run (`install-venvs` fails), the
   remaining venvs are recorded `not_attempted`, and no service is started. Nothing is reported as
   installed that was not read back.
4. A venv without an executable `bin/python` blocks preflight; the dry run lists each planned install.
5. Registration: a service that owns a venv sets `venv` on its plan entry. The reasoning install
   would register `/opt/tessera/venv` the same way; this ADR does not change that installer.

## Consequences

An upgrade can no longer restart a service from an old venv without the receipt saying so. A failed
install leaves services stopped (the migration has already run), like a failed config fix; the receipt
names the backup. Existing plans without `venv` behave as before. Installing needs the operator's pip
index or a local wheel; the command does not fetch anything beyond what pip does for that source.
