# 0119 — The reasoning service installs through one checked, idempotent command

## Status

Implemented. No schema change. Builds on [ADR 0109](0109-reasoning-first-call-readiness.md) (the
first-call readiness check), [ADR 0093](0093-service-credential-install.md) (the stdin-only
installer that owns the API key) and [ADR 0090](0090-model-spend-ledger.md) (spend limits). It adds
no authority: it creates files and an account an operator could create by hand.

## Context

First-call readiness reported `not_ready` on a fresh host because the install the admission checklist
describes had never been run: the service account, `/etc/tessera/reasoning.json`, the state directory,
the credential directory layout and the systemd unit with its `LoadCredential=` lines. An operator
did each by hand, and any slip (a wrong owner, mode or reference) surfaced only at the first call.

## Decision

`tessera reasoning install` is a plan by default and applies only with `--apply`:

1. **Plan first.** `--plan` (the default) reads the host and prints each piece as `ok`, `missing` or
   `DIFFERS` with what apply would do. It writes nothing.
2. **Exactly these pieces, in order:** the service account (a system account with no login and no
   home); the configuration directory (root, 0755); the credentials directory (root, 0700); the state
   directory (the service account, 0700); the prompt file (root, 0600, at most 8192 bytes); the
   profile (root, 0600); the model-credential reference file (root, 0600, exactly the profile's
   `credential_ref`: a reference, not a secret); and the unit `tessera-reasoning.service`, rendered from
   the shipped unit, followed by `systemctl daemon-reload`. A test pins the rendered default to the
   shipped file.
3. **Spend caps from flags.** `--max-cost-cents-per-call`, `--daily-cost-cap-cents` and
   `--daily-token-cap` are written into the profile's `limits`. With an existing profile only those
   three values change and only when given; the rest of the file is untouched. A new profile is built
   from an operator-written `--profile-template` (every non-cap field: model, reference, project,
   control URL, grant, destination and the operator-pinned prices); the command never invents a model,
   an id or a price, and sets `prompt_sha256` from the installed prompt. The result is checked by the
   service's own start-up validator before it is written.
4. **Idempotent and conservative.** Each step acts only when its piece is missing. A managed file that
   exists with other content is `DIFFERS` and is refused unless `--replace` is given. Every write is
   atomic (an exclusive temporary file, fsync, rename), symlinks and non-directories at managed paths
   are refused, and a failed step stops the run and lists what had already changed. Applying again
   changes nothing.
5. **Never a key.** It never reads, prompts for, creates or opens the API key or the agent token. It
   lists what stays a person's act as `pending_operator`: the key (through
   `tessera-service-credential install`, stdin only; that installer keeps the reference file this
   command wrote when it is exactly the reference being installed, so it needs no `--replace`), the
   agent token (the operator's enrollment), the project's spend limits (`PUT /model-spend/limits`), the
   grant and enrolled agent credential the profile names, and admission. It does not create the
   admission marker (`/etc/tessera/reasoning.admitted`), and it neither enables nor starts the service.
6. **It ends with the readiness check.** After a successful apply it runs the ADR 0109 check and
   returns that check's exit status unchanged (0 ready, 10 ready except the key, 11 not fully checked,
   1 not ready). The check is pointed at the stored credentials directory, where the files carry their
   stored names (`reasoning-model-credential`, `reasoning-anthropic-api-key`), so it is told which
   stored file stands for each name the service sees, from the unit's `LoadCredential=` lines. With
   `--control-state-dir`, `--control-url` and a project known, a fresh install ends with exit 10 until
   the key is placed; without them the control checks are unchecked and the result is 11. Run as
   root the check expects the owners the install made (the state directory the service account's, the
   key file root's); running it as the service user needs the follow-on rule that accepts the
   root-owned 0400/0440 files a service manager places, in group root or the service's own group
   (ADR 0109 amendment). Run as the service user, `spend_caps` reads as unreadable (and the result
   is not ready) unless that user can read the control database.
7. **The interpreter is the operator's.** The plan shows whether the unit's interpreter exists, and
   refuses a path the unit cannot reach (under `/home`, `/root`, `/run` or `/tmp`: the unit sets
   `ProtectHome` and `PrivateTmp`). Installing Tessera, with its `mcp` extra, into that virtualenv is
   not done here.
8. **Needs root, or a staging root.** `--apply` needs euid 0; `--root DIR` prefixes every path so the
   whole install can be staged and tested without privilege.

## Consequences

- The install is one command with a plan, instead of a checklist done by hand.
- A host is not admitted by running it: the operator still creates the admission marker, installs the
  key and the agent token, and a person sets the spend limits.
- Not done: prompts, key rotation, installing the Tessera package or its virtualenv, and any
  provider call.

## Tests

`tests/test_reasoning_install.py` (the plan writes nothing, apply builds the exact tree with modes and
owners, a second apply is a no-op, caps change only the limits, refusals, the key and token are never
touched, the unit equals the shipped file) and `tests/test_reasoning_install_cli.py` (the command).
