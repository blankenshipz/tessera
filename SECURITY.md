# Security policy

## Private vulnerability reporting

GitHub private vulnerability reporting is enabled for `blankenshipz/tessera`;
the authenticated API readback was verified during developer-alpha publication.
Use the repository's [Security page](https://github.com/blankenshipz/tessera/security)
→ **Report a vulnerability**, not a public issue. GitHub may require you to sign in.
The earlier private-repository endpoint returned HTTP 404; enablement was verified
after the separately authorized public publication, consistent with GitHub's
public-repository feature availability. This verifies the reporting channel, not
an end-to-end submitted report or a maintainer response-time guarantee.

Do not place vulnerability details, credentials, or sensitive research data in
public issues, pull requests, discussions, or commit messages. Submit sensitive
vulnerability details only through the private reporting flow. Maintainers must
acknowledge a report and coordinate disclosure before requesting additional
reproduction material.

## Scope and limits

Tessera provides installed control/worker processes, a stdio MCP adapter and a
native desktop for the bounded, owned local research bench documented in
[installation](docs/installation.md) and [ordinary collection](docs/ordinary-worker-collection.md).
Observed local operation is not a supported production hosted service,
production hardening, independent-host availability, model-provider integration
or security response SLA. Synthetic fixtures remain distinct from real service
receipts; local bearer credentials are not production federated identity.
Do not test systems, accounts, providers or data you do not own and have explicit
permission to assess. An enabled reporting channel does not establish production
security, provider isolation or a security response SLA.

Security reports should include affected revision, a minimal safe reproduction,
impact, and any mitigation already attempted. Never include secrets or private
research inputs.
