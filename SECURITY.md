# Security policy

## Private vulnerability reporting

The maintainer selected GitHub private vulnerability reporting for
`blankenshipz/tessera`. When enabled, use the repository's **Security → Report a
vulnerability** flow, not a public issue. A recent authenticated API check of the
private repository's reporting endpoint returned HTTP 404. That response leaves
feature availability and enablement **unknown**; it does not establish that
reporting is enabled or disabled. A maintainer must verify an available private
channel before accepting sensitive reports. If GitHub private reporting is not
available for this private repository, select another private channel without
changing repository visibility merely to enable it.

Do not place vulnerability details, credentials, or sensitive research data in
public issues, pull requests, discussions, or commit messages. Until enablement
is verified, report only non-sensitive, high-level concerns
through the repository owner without including exploit details. Maintainers must
acknowledge a report, agree on a private channel, and coordinate disclosure
before requesting reproduction material.

## Scope and limits

Tessera provides installed control/worker processes, a stdio MCP adapter and a
native desktop for the bounded, owned local research bench documented in
[installation](docs/installation.md) and [ordinary collection](docs/ordinary-worker-collection.md).
Observed local operation is not a supported production hosted service,
production hardening, independent-host availability, model-provider integration
or security response SLA. Synthetic fixtures remain distinct from real service
receipts; local bearer credentials are not production federated identity.
Do not test systems, accounts, providers or data you do not own and have explicit
permission to assess. Private reporting also remains an external release gate.
The endpoint result above does not establish feature status; do not infer enabled/
disabled or change repository visibility as a workaround.

Security reports should include affected revision, a minimal safe reproduction,
impact, and any mitigation already attempted. Never include secrets or private
research inputs.
