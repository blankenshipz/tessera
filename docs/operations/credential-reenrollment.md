# Re-enrolling an expired provider credential

Contract: [ADR 0103](../decisions/0103-credential-expiry-health.md) and
[credential health](../credential-health.md). The command line is the only place a secret is
entered. The control API and the MCP tools never accept, store or transmit one.

## When a credential expires

Tessera reports credential metadata (never a value). Two places show the problem before a run
fails deep inside it:

- `tessera_credential_health` (or `GET /credential-health`) lists each credential with a state:
  `warning` (under 24 hours left), `expired`, `revoked` or `version_mismatch`. An item looks like
  `{"provider_id": "salad", "profile": "...", "reference": "credential-ref:salad-management",
  "version": 2, "state": "expired", "reason": "expired 120 seconds ago"}`.
- Campaign admission refuses a stage whose provider credential is `expired`, `revoked` or
  `version_mismatch` with a 409 naming the reference, version and expiry, and a running stage stops
  with `credential_expired`, `credential_revoked` or `credential_version_mismatch`.

## The one-command flow (Salad management credential)

Run as the user that owns the private credential directory, in a terminal you control.

1. Create a new key in the provider's console. A person does this; Tessera cannot.
2. Put the key in an owner-only file (mode 0600), or pipe it from your secret manager. It is read
   from standard input only: never an argument or environment variable, and never from a terminal.
3. Run:

   ```
   tessera credentials reenroll salad --credential-directory DIR \
     --reference credential-ref:salad-management --version N --expires-in-hours 72 \
     --profiles-file PROFILES < /path/to/secret-file
   ```

   `--profiles-file` supplies the Salad organization for the read-only check and the version the
   profile pins (or pass `--organization ORG`); without either the command refuses
   (`validation_target_unknown`). `--version N` stores exactly version N instead of the previous
   version plus one. `--expires-in-hours` defaults to 72 (a lifetime of 24 hours or less is reported
   by the health check as a warning, not ok). Also optional: `--receipt-directory DIR`. The
   command validates
   the key with a read-only provider call before storing anything, stores the next version, writes a
   receipt and reports whether the credential-expiry-health finding is cleared. Exit 0 means done,
   2 means refused with a closed category (nothing was stored), 3 means the new version IS stored
   but its receipt file could not be written (the receipt is printed on stdout instead), 1 means
   unexpected.
4. The receipt records the provider, reference, version and expiry metadata. It never contains the
   secret.
5. Replacing the key here does NOT revoke the old one at the provider: after the services have
   restarted and use the new version, delete the previous key in the provider's console (the
   command reminds you).
6. If the profile pins a `credential_version`, pin the new one:
   `tessera providers update --section salad-profile --key PROFILE --entry-json '{"credential_version": N}'`.
7. Restart the control, guardian and executor services as the command prints, so they resolve the
   new version.
8. Confirm with `tessera_credential_health`: the item is `ok` again.

## What the MCP tool shows

`tessera_credential_reenroll_command` (read-only; registered only for a credential with
`provider_trust:read`, like `tessera_credential_health`) reads the same health route and, for each
credential that is `expired`, `revoked`, `version_mismatch` or `warning` (optionally for one
`provider`), returns the provider, profile, reference, state, reason, pinned version, the exact
command for the owner, ordered steps and a note. The credential directory and the secret file stay
placeholders (`<CREDENTIAL_DIRECTORY>`, `/path/to/secret-file`), and every interpolated value is
shell-quoted; a record with an unsafe reference is refused. For `version_mismatch` the command is the
profile pin instead of an enrollment. The tool takes only an optional `provider` id: it has no
parameter that could carry a secret, rejects any other argument, and changes nothing. An agent can
tell the owner what to run; only the owner can run it.

## Adding another provider

A provider that reports credential health through its `credential_health` hook shows up in the
health route and in the tool without change. For the command to re-enroll it, register one adapter
for the provider in the credentials command (its read-only validation call and its reference
naming); the MCP tool needs no change.

## Troubleshooting

A refusal prints a closed category on stderr, exits 2, and stores nothing. Each category and what
to do:

| Category | What to do |
| --- | --- |
| `stdin_is_a_terminal`, `bad_secret_shape`, `secret_entry_failed` | Redirect a file or pipe one secret (one line, at most 4096 bytes). |
| `validation_rejected` | The provider refused the key (HTTP 401/403); create a fresh key in the console and check the account. |
| `validation_provider_unavailable` | The provider gave no usable answer (an outage, a gateway page or another status): this is not a verdict on the key; retry later. |
| `validation_organization_invalid` | The organization name from `--organization` or the profile is malformed. |
| `validation_credential_unavailable`, `validation_malformed_response`, `validation_failed` | The key could not be sent as is, or the answer was not understood; re-check the key (no spaces, one line) and retry. |
| `validation_target_unknown` | Pass `--profiles-file` (a profile naming this reference) or `--organization`. |
| `reference_not_found`, `reference_revoked` | The reference must exist and not be revoked. A revoked reference is never re-enrolled (the revocation is deliberate, and is also refused again at the store if it lands mid-run: `enrollment_rejected`); enroll a new reference and point the profile at it. |
| `credential_store_unavailable` | Fix ownership and mode of the credential directory (owner-only) and run as its owner. |
| `version_not_greater`, `version_exhausted` | `--version` must exceed the stored version; omit it for the next one. |
| `bad_expiry` | Use a whole number of hours in range. |
| `profiles_unreadable`, `profile_reference_unknown`, `profile_pin_ambiguous` | Fix the profiles file: exactly one profile should name this reference. |
| `receipt_directory_invalid` | Use an existing absolute directory (checked before anything is read or stored). |
| exit 3, `stored_receipt_unwritable` | The new version IS stored; only the receipt file could not be written (the directory became unwritable or the file already exists). The receipt is printed on stdout: keep it, then clean up the directory. |
| `unknown_provider` | Only providers with a registered adapter can be re-enrolled. |
| `enrollment_rejected` | The store refused the write; nothing was changed, re-check the directory. |
| Finding not cleared after success | Pin the new version in the profile, restart the services, re-read health. |
| Unexpected (exit 1) | Nothing is stored after a refused validation; re-run once, then inspect the service logs. |
