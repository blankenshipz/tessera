# 0093 — Installing a service credential reference without exposing the secret

## Status

Accepted and implemented (library and command). No schema change. Builds on the
systemd `LoadCredential` start-up mapping of the remote agent platform (`deploy/remote-agent-platform`)
and, for the first user, the direct model-API destination of the reasoning service. No live call was made
to build or test this record.

## Context

A model API key reaches a service in the ways keys usually leak: typed on a command line (shell history,
process listings), exported in an environment (inherited by children, visible in process environments),
put in a unit's `Environment=` or an environment file (readable by anyone who can read unit files),
pasted into a hand-created file with the wrong owner or mode, or echoed into a log while debugging.
Tessera already says credentials are references (a systemd credential file, never an environment value),
but nothing made the safe path the easy path: the operator created `/etc/tessera/credentials/...` by hand.

## Decision

1. **Stdin only.** `tessera-service-credential install --service SERVICE --name NAME --owner LABEL
   --reference LABEL [--replace]` reads the secret from standard input: a pipe, or a no-echo prompt on a
   terminal. There is deliberately no option that takes the value, and the secret is never read from the
   environment. `OWNER` and `REFERENCE` are operator labels, not account names or secrets.
2. **Refuse a general environment key.** The command refuses when `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_API_KEY` is set in its own environment, or is named in the service's
   unit, its drop-ins or an environment file they reference (names are checked; values are not read). A
   credential that is also reachable as an environment variable is not a credential reference.
3. **Root-owned systemd credentials, both of them.** For the reasoning service's Anthropic destination
   it writes two files in `/etc/tessera/credentials`, each a root-owned regular file, mode 0600, single
   link, written atomically (a temporary file created exclusively in the same directory, fsynced, renamed
   over the target, the directory fsynced; `--replace` is required to replace):
   - `reasoning-anthropic-api-key`, the key, loaded by
     `LoadCredential=anthropic-api-key:/etc/tessera/credentials/reasoning-anthropic-api-key`;
   - `reasoning-model-credential`, the reference `anthropic-api-key://OWNER/REFERENCE` on one line, loaded
     by `LoadCredential=model-credential:/etc/tessera/credentials/reasoning-model-credential`. The start-up
     mapper compares it with the profile's `credential_ref` and refuses a mismatch, so the installer writes
     it too. If the profile file is given (`--profile FILE`, default the service's `/etc/tessera/reasoning.json`
     when it exists) and its `credential_ref` differs from the reference built from `--owner` and
     `--reference`, the install is refused (`reference_mismatch`) before anything is written. systemd places
     both under `$CREDENTIALS_DIRECTORY` for that service only.
4. **One key shape, shared with the runtime.** The installer accepts exactly what the runtime accepts: 1 to
   512 printable ASCII characters (`\x21`-`\x7e`), with one trailing newline stripped. The pattern is the
   Messages client's own, so a key that installs is not refused at call time for its shape. An empty, longer,
   space-bearing or non-ASCII value is refused (`invalid_credential_shape`) with no bytes in the message.
5. **Verify before and after.** If the service's unit exists without both exact `LoadCredential=` lines the
   install is refused before anything is written. After the write each file is re-read and the key's
   fingerprint must match what was installed. The unit scan is best effort: it reads the unit fragment and
   drop-ins under `/etc/systemd/system` and `/usr/lib/systemd/system` (names only, never values). It does not
   see units under `/run`, top-level `service.d` drop-ins, `PassEnvironment=`, continuation lines or
   `systemctl set-environment`. The real enforcement is the start-up refusal below, not this scan.
6. **A receipt with the reference and fingerprint only.** A hash-chained line is appended to
   `credential-install.receipts.jsonl` (mode 0600) with: time, service, credential name, the reference,
   the installed paths, the fingerprint (`sha256:` plus 16 hex digits of the secret's SHA-256), size, the
   installer's uid, whether the unit declares the credential, and whether an existing file was replaced.
   The secret, and any prefix of it, appears in no file, receipt, log line, error message or argument list.
   A failed install leaves no partial file; a failed `--replace` restores the previous key from a backup
   this run created (named with a random suffix, so a stale backup is never restored).
7. **`check`, `list` and recovery.** `check` verifies each installed file (mode, owner, shape, fingerprint)
   and the unit declarations, prints names and fingerprints, and exits non-zero (78) if a file is unsafe or
   a declaration is missing. `list` prints names and fingerprints from the receipts. `receipts verify`
   recomputes the chain. A broken chain (an edited line, a truncated tail) refuses every further install
   (`store_unavailable`) until an operator runs `receipts archive-broken`, which moves the broken file aside
   under a dated name, appends nothing to it, and starts a new chain whose first record names the archived
   file's last good hash. Archiving is an owner action and is itself recorded.
8. **`--user-store`.** Writing as a non-root user is possible only with `--user-store` (and a directory the
   caller owns, via `--credentials-dir`): it exists for tests and for a non-root deployment, never changes
   the root requirement of the default store, and is shown in the receipt (`installed_by_uid`).
9. **The start-up admission is the existing mapper.** The reasoning service's start-up
   (`tessera_agent.service_startup`) already refuses to start (exit 78, no automatic restart) on a general
   model-key variable in its environment (even empty), a missing or mismatched `model-credential`
   reference, and a missing or non-private `anthropic-api-key` file. This record adds no second admission:
   the installer's output is built to satisfy that mapper, and the tests drive the installer's files through
   the real `launch_spec` and `main`.
10. **Generic.** The services and credentials the command knows are entries in a `SERVICES` registry (file
    name, systemd credential id, forbidden environment names, companion reference file). A new service or
    vendor credential is a data entry, not new code.

## Consequences

- The only place the secret exists is the pipe it is piped through, the root-owned file and the service's
  own credential directory; the receipt lets a reviewer tell which key is installed without seeing it.
- Hand-edited unit environments and stray environment variables are refused at install time and at start.
- Following the documented flow yields a service that the existing start-up mapper admits: the key file,
  the reference file and the profile agree, or the install is refused.

## Limits

- A root operator and the service itself can read the key. This is not a vault, an HSM or a defence
  against host root.
- The fingerprint is a hash of the secret. For a high-entropy API key it identifies the key without
  revealing it; for a low-entropy secret it would be guessable. The shape check does not measure entropy.
- Rotation is `install --replace` followed by a service restart. Nothing verifies the key with its provider,
  and nothing here makes a model call.
- The command can only check the unit files it can read, and only best effort (decision 5); a service
  started by other means is outside it, which is why the start-up refusal is the enforcement.
- The receipt chain detects an edited or truncated middle, not a deleted file or tail; its last hash can be
  copied elsewhere by an operator who wants to anchor it.

## Tests

`tests/test_service_credentials.py` (stdin-only reading, no value option, environment and unit refusals,
the shared key shape, the root-ownership path (`fchown` to root and refusal for a non-root caller, with the
privileged calls monkeypatched), hard-link refusal, the companion reference file and `reference_mismatch`,
atomic write, rollback, receipt contents, chain, `receipts verify` and `archive-broken`, `check` exit status,
no secret anywhere) and `tests/test_service_startup_credential_admission.py` (the installer's files through
the real `launch_spec` and `main` with an Anthropic profile: admitted; a different reference, a 0640 key, a
missing key and a general key variable each refused; a 513-character key refused by the installer and by
the runtime's own pattern).
