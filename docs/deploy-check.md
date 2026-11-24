# Deploy check

`tessera deploy-check` turns the steps a deploy used to need a person for into one checked
command. It is read-only unless you name a fix with `--apply`, it reads no secret, it never
contacts a provider and it spends nothing. Design and limits: [ADR 0098](decisions/0098-deploy-check.md).

```
tessera deploy-check \
  --state-dir STATE_DIR \
  --executor-config EXECUTOR_CONFIG [--live-config LIVE_CONFIG] \
  --providers-file PROVIDERS_FILE \
  [--max-backup-bytes N] [--smoke] \
  [--apply drop_removed_keys --apply migrate --backup-confirmed --receipt-dir RECEIPTS] \
  [--format json] [--report-file REPORT]
```

Every path is absolute and belongs to you; each option is optional, and an area you do not name is
not checked. The exit status is 0 when nothing failed, 1 when something did, 2 for a usage error.

| Check | What it reads | Fix |
| --- | --- | --- |
| `state.schema` | the control database's schema version, read-only | `--apply migrate` (with `--backup-confirmed`), or `tessera migrate` |
| `backup.cap` | the estimated backup size against the bound `tessera backup` would use | pass `--max-backup-bytes N` to `tessera backup` and `restore`; the bound has no stored setting |
| `config.N.keys` | a config's keys: ones a release removed, ones no release defines, and its own validation | `--apply drop_removed_keys` removes only the removed keys; an unknown key is yours to edit |
| `config.N.source_pin` | an executor config's `source_revision` is the pin the live check accepts for the installed source | `--apply refresh_source_pin --source-commit <40hex>` re-pins it in place (receipted); `deploy-upgrade` does this with the plan's `source_revision`. Only the digest is measured from the install; the commit is operator-asserted. Run from a repository checkout (`tests/` and `src/` in the working directory), the command refuses a commit that is not the checkout's head, before any fix; with no checkout it cannot compare and accepts the commit as given. If a later upgrade step (start, smoke) fails after the re-pin, the config copies from before the fixes are in the upgrade's `<name>-configs` directory beside the backup (made before the first config fix, after a successful migrate) |
| `providers.file`, `providers.audit` | the providers file and its audit chain (ADR 0081) | register through `tessera providers` |
| `providers.frame_verdict_read` | when the providers file configures `salad_profiles`: that `salad_guardian_read` is set and a valid guardian read config, and, with `--guardian-status-file FILE` (a saved copy of the guardian's `status` reply; the check never contacts the guardian), that the reply carries `frame_record` | upgrade the guardian first so its `status` returns `frame_record`, then set `salad_guardian_read` (or `tessera serve --salad-guardian-read FILE`) |
| `providers.executor_profiles` | each profile an executor config names, in its profiles file | register the profile through `tessera providers` |
| `smoke.cpu` (`--smoke`) | a synthetic container: no network, read-only root, no capabilities, a pinned local image that is never pulled | `docker pull` the pinned image yourself |

`providers.frame_verdict_read` fails (exit 1) with `detail.reason` `config_missing`, `config_invalid`,
`guardian_version_missing` or `guardian_status_unreadable`. Without it, Salad frames silently stop
auto-settling and every guardian-bounded hold waits for a human charge (ADR 0041). Deploy order:
upgrade the guardian, confirm its status carries `frame_record`, then give control the config. Without
`--guardian-status-file` the guardian version is not checked and the finding says so.

An applied fix writes an owner-only receipt (`tessera.deploy-fix-receipt.v1`) to `--receipt-dir`.
It records the digests before and after and the key names or schema versions that changed, never a
path or a value. Applying again changes nothing and writes no second receipt.

An agent or a person can read the control-side part of the same report (schema and the providers
file) without file access: `GET /admin/deploy-check`, or the MCP tool `tessera_deploy_report` when
the operator enables the provider tools. Config files, the backup bound and the smoke are local to
the operator's host and stay with the command.
