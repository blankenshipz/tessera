# Credential health and rotation

Contract: [ADR 0103](decisions/0103-credential-expiry-health.md). Tessera reports the expiry of the
credentials its providers depend on (metadata only, never a value), refuses work that needs an
expired one, and has a supported way to rotate one.

## States

| State | Meaning | Blocks a stage |
| --- | --- | --- |
| `ok` | valid, more than 24 hours left | no |
| `warning` | valid, under 24 hours left | no (an event records it) |
| `expired` | past its expiry | yes |
| `revoked` | revoked in the store | yes |
| `version_mismatch` | the profile pins a `credential_version` the store does not hold | yes |
| `unknown` | the control service cannot read the credential store | no (reported only) |

## Where to look

- `GET /credential-health` (a human, or an agent with `provider_trust:read`): every credential, its
  state, version, expiry and seconds remaining.
- `GET /operator/health`: the same summary among the other health checks.
- MCP: the read-only tool `tessera_credential_health`.

The control service needs read access to the private credential directory, set with
`serve --salad-credential-directory DIR` (the directory must be owned by the service user, mode 0700).
Without it every state is `unknown`.

## What a refusal looks like

At submit or approval a stage on a provider with a blocking state is refused (409) with the reference,
version, expiry and the rotation hint. During placement the candidate is skipped as
`credential_unavailable`, with that reason on the placement receipt. A running single-provider stage
fails before any hold; the campaign stop reason is `credential_expired`, `credential_revoked` or
`credential_version_mismatch`, and the event `campaign.stage_credential_blocked` records it.

## Rotating, step by step

Run as the user that owns the private credential directory, in a terminal you control:

1. `python -m tessera_operator.salad_enroll rotate --credential-directory DIR --reference credential-ref:NAME
   --expires-in-hours N`. The new key comes from a hidden prompt; to pipe it instead add `--secret-stdin`
   and supply it on stdin (never as an argument or environment variable). It enrolls the next version.
2. Set the profile's `credential_version` to the version printed, for example
   `tessera providers update --file ... --section salad-profile --key NAME --entry-json '{"credential_version": N}'`.
3. Restart the control, guardian and executor services so they resolve the new version.

To extend the same key without changing it (at most 24 hours per call): `renew --credential-directory DIR
--reference credential-ref:NAME --expires-in-hours N`, then step 2 and 3 with the printed version.

Neither command is reachable through the API or MCP, prints a secret, or accepts one as an argument.
