# Local protocol compatibility gate (source RC, intent revision 5)

Run the opt-in harness outside default pytest/CI:
Set `EVIDENCE_DIR` to a new private directory outside the source tree; output
receipts are local artifacts, not package inputs.

```sh
PYTHONPATH=src <isolated-sdk-python> dev/check_protocol_compatibility.py \
  --receipt "$EVIDENCE_DIR/source-protocol.json"
```

Prerequisites: project test dependencies and exact `a2a-sdk[http-server]==1.2.1`,
`mcp==1.26.0`. The harness requires the source checkout's existing synthetic
resource/registration fixtures. It launches the shipped Python service modules
(`tessera_control.a2a_peer`, `tessera_mcp.server`) with a minimal child environment,
not mock peer/server replacements. It does **not** invoke the research model
consumer or authenticate to any provider. Only temporary enrolled application
credentials, private files and synthetic human-approved public output are used.
No existing authentication/profile files or active coding services are touched.

The backend uses real loopback Uvicorn HTTP and temporary SQLite. Two independent
A2A processes have separate durable ledgers, actual SDK JSON-RPC 1.0 discovery,
reverse narrowed delegated reference exchange, cross-ledger denial, registered
research-generated output delivery linked to the existing typed author capture,
changed-pin denial, receiver restart retaining task/session pins, explicit planner
ACK/replay, and pending-task cancellation fencing ACK. Five receiver transport
records are delivered to the real collector and acknowledged. The already-existing
author capture fixture supplies the synthetic approved source; the harness does
not add new capture fields or impersonate the writer with the receiving credential.

Real MCP subprocesses negotiate session-based stdio `2025-11-25`, list the exact
catalog, and exercise status, authority status and recipient generated-output
metadata reads across reconnect, plus invalid-disclosure denial. Separate critic
stdio sessions replay the original typed registration write with its unchanged
key/payload across reconnect; they do not mint another output or human approval.
Advertised tool names are not proof every mutation is authorized by the fixture's
scopes. No approval/issue-grant tool is advertised. No experiment dispatch,
provider call or running-Job termination is attempted.

Ports are selected by binding available loopback sockets, then SDK discovery
checks the expected peer identity. Inspect existing listener ownership before the
run; the harness never kills a port owner. It stops/waits only its own peer process
objects; Uvicorn fixture sockets/threads and MCP stdio contexts close on all exits.
Receipt native peer return codes include owned SIGTERM cleanup (typically `-15`);
these are not unexplained peer crashes. The MCP SDK context does not expose native
child exit codes; those remain explicitly unknown rather than invented success.

The redacted receipt records exact protocol/SDK versions, catalog, stage names,
opaque task/principal IDs, native peer exit codes, and source-file SHA256 pins.
No tokens, credential hashes, private config bytes, raw public text, prompts,
tool payloads or hidden reasoning are included. A failure prints a short pytest
traceback instead of fixture locals or raw server output. Source receipt is
provisional, not a final candidate acceptance or scientific-validity receipt.

## Final release prerequisites

Installed mode is implemented but awaits the parent's final wheel. Run:

```sh
<harness-python> dev/check_protocol_compatibility.py \
  --installed-python /PRIVATE/candidate/bin/python \
  --installed-bin-dir /PRIVATE/candidate/bin \
  --wheel /PRIVATE/artifacts/tessera_control-VERSION-py3-none-any.whl \
  --wheel-sha256 INDEPENDENTLY_SUPPLIED_64_HEX_SHA256 \
  --receipt "$EVIDENCE_DIR/installed-protocol.json"
```

The outer process preflights the explicit candidate and re-executes this harness
under candidate Python with `-I` and no source `PYTHONPATH`. Fixture/helper imports
may come from checkout `tests`/`dev`; Tessera imports, including backend code, must
resolve within the candidate's own site-packages. Both actual service subprocesses
use installed `tessera-mcp`/`tessera-a2a-peer` command scripts with exact candidate
Python shebangs. Missing/wrong entrypoints, source/editable Tessera `.pth`, escaped
or symlinked product origins, wheel hash mismatch and missing/mismatched wheel
`direct_url.json` installation evidence fail closed. Installed product bytes must
match the supplied wheel; explicit third-party `.pth` references never waive
Tessera origin checks. Imports are re-audited at receipt time. Wheel SHA, installed
origins/entrypoint hashes and checkout source hashes are separate receipt fields.
The prior source receipt cannot be overwritten by installed mode. These guards'
unit tests do not establish an installed protocol run.

Standard UV wheel installation may record `archive_info:{}` without an installer
archive hash. This is accepted **only** when `INSTALLER` is `uv`, PEP610 contains
exactly local `url`/`archive_info`, the canonical file URL equals the independently
SHA256-verified supplied wheel, and every installed product byte plus its wheel
and installed RECORD hash/size match. Name/version and METADATA/WHEEL/entry-point
metadata must also match; extra product files, path escapes and source origins
fail. Editable/directory/remote/extra metadata is not admitted. If any archive
hash is present, all supplied hashes must agree with the independent SHA; empty
hash maps or conflicting values are not treated as hashless UV metadata. Receipt
reports `installer_archive_sha256:null` and the distinct UV local-URL/independent-
wheel/installed-bytes binding basis, never a fabricated installer hash.

- Bind a reproducibly built wheel/image artifact and clean-install environment.
- Rerun all eight stages in installed mode using the final exact wheel. The earlier
  source-tree receipt cannot replace that gate; installed-mode implementation alone
  is not a passing installed protocol receipt.
- Retain real approved public visibility/harness admission, generated planner/critic
  inference and consumer integration, full research MCP workflow/experiment, active
  run restart/running-job cancellation, off-host and device/laptop-off gates.
- Preserve financial controls and prior auth/service denials. No commit, publication,
  deployment, inference admission or active binary replacement is implied here.
