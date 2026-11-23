# Installing a service credential

A service credential (for example the Anthropic API key of the reasoning service) is installed as a
**systemd credential reference**: a root-owned 0600 file that the unit loads with `LoadCredential=`. The
secret never appears on a command line, in an environment variable, in a unit file, in a log or in a receipt.
Contract: [ADR 0093](decisions/0093-service-credential-install.md).

## Install, step by step (the reasoning service's Anthropic key)

1. **Unit.** The unit must load both credentials. The base unit in `deploy/remote-agent-platform` already
   does; a unit of your own needs both lines, and the install is refused if the unit exists without them:

   ```ini
   LoadCredential=model-credential:/etc/tessera/credentials/reasoning-model-credential
   LoadCredential=anthropic-api-key:/etc/tessera/credentials/reasoning-anthropic-api-key
   ```

2. **Profile.** `/etc/tessera/reasoning.json` names the key by `"credential_ref":
   "anthropic-api-key://OWNER/REFERENCE"` (`OWNER` and `REFERENCE` are labels you choose, not secrets).
3. **Install.** As root, with the secret on standard input only (a pipe from your secret provisioner, or type
   it at the no-echo prompt on a terminal). There is no option for the value, and the command refuses to run
   with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_API_KEY` set:

   ```sh
   sudo tessera-service-credential install \
     --service tessera-reasoning --name anthropic-api-key \
     --owner OWNER --reference REFERENCE   # the secret: stdin only; add --replace to rotate
   ```

   It writes **both** files (the key and the `model-credential` reference), each root-owned, mode 0600,
   atomically, and refuses (`reference_mismatch`) if the profile's `credential_ref` is not
   `anthropic-api-key://OWNER/REFERENCE`. The key must be 1 to 512 printable ASCII characters, the same rule the
   runtime applies.
4. **Check.** `check` verifies the files and the unit; it exits non-zero if a file is unsafe or the unit does
   not declare a credential. Then start the service (the existing start-up mapper admits it, or refuses with a
   sanitized reason).

The receipt, `credential-install.receipts.jsonl` (mode 0600, hash-chained), records the time, service,
credential, reference, paths, fingerprint (`sha256:` and 16 hex digits), size, installer uid, whether the unit
declares the credentials and whether a file was replaced. The secret is not in any of it.

## Check and list

```sh
sudo tessera-service-credential check --service tessera-reasoning   # files, fingerprints, unit declarations; non-zero on a problem
sudo tessera-service-credential list --service tessera-reasoning    # names and fingerprints from the receipts
```

## Withdraw (roll back an install)

To take a key out of service, do not delete files by hand. As root:

```sh
sudo tessera-service-credential withdraw \
  --service tessera-reasoning --name anthropic-api-key \
  --owner-label WHO [--reason TEXT]
```

It removes the key file and its `model-credential` reference file together (both or neither) from the checked
credential directory, through the same registry `install` uses. It refuses symlinks, hard links and anything
outside the directory, and never reads or prints the key. It is idempotent: a second run records
`already_absent`. It appends a hash-chained receipt with who withdrew, which credential reference, when and the
outcome; the reference and fingerprint come from the earlier install receipt, not from the file.

Afterwards `check` reports the credential as not configured, and the unit (whose `LoadCredential=` names that
file) will not start until a credential is installed again. Stop or disable the service first if it should stay
down. Withdrawing does not revoke the key at the provider; do that separately.

## If the receipt chain is broken

Every install and withdraw verifies the chain first. If a receipt line was edited or the tail truncated, they are
refused (`store_unavailable`). Look with `receipts verify`; if you accept the break, run
`sudo tessera-service-credential receipts archive-broken --service tessera-reasoning`. It moves the broken
file aside under a dated name and starts a new chain whose first record names the archived file's last good
hash. Nothing is deleted.

## Non-root stores

`--user-store` with `--credentials-dir DIR` installs into a directory the caller owns (tests, or a non-root
deployment). The default store stays root-only.

## What the service does at start

The reasoning service's existing start-up mapper refuses to start (exit 78, not restarted) when any of
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_API_KEY` is in its environment (even empty), when the
`model-credential` file is missing or does not equal the profile's `credential_ref`, or when the
`anthropic-api-key` file is missing or not a private regular file. That start-up refusal is the enforcement;
the install-time unit scan is best effort (it reads `/etc/systemd/system` and `/usr/lib/systemd/system`
fragments and drop-ins and does not see `/run` units, top-level `service.d`, `PassEnvironment=`,
continuation lines or `systemctl set-environment`).

## Limits

A root operator and the service itself can read the key; this is not a vault. The fingerprint identifies a
high-entropy key without revealing it; a guessable secret would not be protected by it. Nothing verifies the
key with its provider.
