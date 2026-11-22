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
| `providers.file`, `providers.audit` | the providers file and its audit chain (ADR 0081) | register through `tessera providers` |
| `providers.executor_profiles` | each profile an executor config names, in its profiles file | register the profile through `tessera providers` |
| `smoke.cpu` (`--smoke`) | a synthetic container: no network, read-only root, no capabilities, a pinned local image that is never pulled | `docker pull` the pinned image yourself |

An applied fix writes an owner-only receipt (`tessera.deploy-fix-receipt.v1`) to `--receipt-dir`.
It records the digests before and after and the key names or schema versions that changed, never a
path or a value. Applying again changes nothing and writes no second receipt.

An agent or a person can read the control-side part of the same report (schema and the providers
file) without file access: `GET /admin/deploy-check`, or the MCP tool `tessera_deploy_report` when
the operator enables the provider tools. Config files, the backup bound and the smoke are local to
the operator's host and stay with the command.
