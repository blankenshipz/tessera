# Tessera

**A research workspace for turning goals into bounded experiments, linked evidence,
and human decisions.** Tessera keeps the design, approvals, attempts, results and
research ledger together—without treating an agent's proposal as permission to run.

**Developer alpha · local evaluation, not a production platform.** The supported
starting point is a local control service and macOS desktop. A bounded CPU baseline
has run on an owned Docker Desktop Kubernetes bench; general remote execution,
integrated agent reasoning and scientific acceptance are not demonstrated.

## A look inside

These are cropped, sanitized captures of the actual renderer connected to a real
local backend, **with platform selection/save interactions simulated**. Private
record identifiers are masked; no UI or results were fabricated. These are not
native macOS picker/save proof or screenshots of an autonomous agent conversation.

### Record the experiment design

![Experiment design with its goal, success criterion and retained design history](docs/assets/alpha-design.png)

An immutable design revision records intent, not execution authority. This earlier
revision explicitly says execution is not yet authorized; the later baseline used
a separately bound and approved revision.

### Inspect real baseline results

![Real baseline test metrics and quarantined, unclassified artifact inventory](docs/assets/alpha-results.png)

One operator-authorized CPU baseline on synthetic data produced test MSE 131.0623
and MAE 9.8565 over 1,667 rows. Scientific validity is not established; there are
no human conclusions. The output remains quarantined and unclassified.

### Review before retrieval and export

![Quarantine-retention review showing late output and scientific validity not assessed](docs/assets/alpha-review.png)

The renderer read an existing operator-recorded review; this capture is not a new
personal UI approval. Late status remains true. Separate custody release enabled
exact-byte retrieval and a reproduction-bundle **record** export in the simulator;
it did not classify data, accept science or save a filesystem bundle.

## Supported workflow

The [quickstart](docs/quickstart.md) follows this bounded local journey. Execution
is a separate API/operator handoff, not a one-click desktop operation.

```mermaid
flowchart LR
  A[Connect to local service] --> B[Project and versioned design]
  B --> C[Separate human approval and execution grant]
  C --> D[Operator admission and CPU baseline dispatch]
  D --> E[Measurements and quarantined artifacts]
  E --> F[Review retention and separate custody release]
  F --> G[Exact result bytes and bundle record export]
  G --> H[Reconnect to durable records]
```

### Agent-assisted research: components, not an integrated demonstration

```mermaid
flowchart LR
  A[Agent proposes bounded plan] --> B[Human reviews exact revision and scope]
  B --> C[Grant and admission checks]
  C --> D[Direct Agent Sandbox adapter]
  D --> E[Persist outputs under custody rules]
  E -. Integrated reasoning and critique unverified .-> F[Agent critique proposal]
  F --> G[Human decision in research ledger]
```

Proposal/review records and the opt-in [direct Agent Sandbox adapter](docs/agent-sandbox.md)
have isolated component tests. The adapter targets upstream **v1.0.6** one-shot,
network-none Sandboxes; it does not replace batch Jobs. No live CRD/controller or
Sandbox runtime acceptance is claimed. Warm pools, read-only source integration,
input/reasoning credential delivery and a complete agent reasoning/critique loop
are not available in this demonstrated journey.

## Quickstart

Requires macOS, Python 3.11+, uv, Node **22.12.0+** and npm. Python's SQLite must
provide built-in deterministic, innocuous `json_extract` for defensive recovery;
an incompatible build is rejected before control-state writes. See
[runtime prerequisites](CONTRIBUTING.md#setup). In a fresh state directory:

```sh
uv sync --locked --extra mcp
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
```

In another terminal:

```sh
npm ci --prefix desktop
npm start --prefix desktop
```

Connect to `http://127.0.0.1:8765` and select the private `.tessera/owner.header`
through the app's file picker; never paste its contents. Initialize only once;
existing state needs the documented backup/migration procedure. Follow the
[screen-by-screen quickstart](docs/quickstart.md) for review/export and troubleshooting.
Native installation and picker/save acceptance require their own checks;
simulator evidence does not close those gates.

## Architecture

**Desktop → control plane → execution plane.** The desktop is a client; the control
service owns revisions, authorization and durable records; workers perform
explicitly admitted execution. Approvals, artifact custody, cost settlement and
scientific acceptance are separate decisions. External trackers are not authority.

See the [documentation guide](docs/README.md) for user, operator and developer
navigation, [capabilities and limits](docs/implemented-capabilities.md) for current
status, and [DESIGN.md](DESIGN.md) for authoritative contracts.

## Limits and safety

- Provider activation is disabled by default; no hosted service or production
  organization authentication is promised.
- Native OS dialogs, independent-host operation, live Sandbox isolation, integrated
  model reasoning, cost settlement and scientific validity need separate evidence.
- Results can be late, quarantined or unclassified even after authorized retrieval.
  Successful execution is not a scientific finding or publication approval.
- This source alpha is not a signed binary release. See the
  [release checklist](docs/release-checklist.md) for remaining gates.

## Contributing and security

Use [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and bounded live fixtures.
Read [SECURITY.md](SECURITY.md) before reporting vulnerabilities: do not post secrets,
research data or exploit details publicly; GitHub private vulnerability reporting
is enabled through the repository's Security page. Tessera source is [MIT licensed](LICENSE); bundled
third-party runtimes retain their own license notices.
