# Supervised partial process/artifact identity exporter

`tessera_agent.research_identity.OwnedResearchIdentity` measures an explicitly owned
live direct-child `subprocess.Popen` handle and separately approved nonsecret
immutable artifact references. It never discovers arbitrary PIDs, starts a harness,
reads command lines/environment/credentials/provider headers, or admits a model.
Only the supervisor owning the child can create and use this in-memory private
receipt; a supplied JSON record/PID cannot restore it after supervisor restart.

Production caller must provide a canonical caller-owned private artifact root,
an actual dedicated research child handle, and at most eight `ArtifactReference`
entries. Exactly one entry is `executable`, with actual OS executable-path readback
matching that approved reference. Optional `build` and `sdk-metadata` entries each
carry an opaque ID, relative allowlisted path and independently approved SHA256.
References are explicitly approved **nonsecret bytes**, not source/config/auth files.
Known source/config suffixes and credential/OAuth/secret/token/password path markers
are rejected before opening; this is defense in depth, not a content classifier.
The supervisor remains responsible for approving genuinely nonsecret artifacts.
No reference inference, filesystem scanning or response-provided trust roots occur.

Targeted parent/start metadata is read for that handle only; macOS native libproc
or Linux `/proc/<owned-pid>/exe` supplies only executable path, never command/body.
The live unreaped direct-child handle prevents accepting a recycled PID; parent,
handle PID, supervisor PID and start/executable identity must remain bound. Polling
exit or changed identity holds. Paths are opened component-by-component beneath
the approved root without following symlinks; caller ownership, non-writable-by-
others modes, regular files and single links are required. File hashes/size/stat
stability and live process identity are rechecked for each observation. Reads are
bounded to 64 MiB per executable/build and 64 KiB per SDK metadata; nothing executes.

Only standard approved installed `*.dist-info/METADATA` supplies package/version
evidence (`mcp`, `opencode`, `opencode-ai`). This measures the approved installed
metadata bytes, **not proof that the running harness launched that SDK**. Similarly,
an approved build hash does not prove harness-build linkage or effective config.
The caller must separately establish executable/build/SDK relationships truthfully.

Output binds a fresh bounded nonce (replay rejected), 30-second validity, current
PID/parent/start/opaque supervisor receipt and actual artifact hashes/size/version.
No paths or raw artifact bytes are returned. `complete` is always `false`; missing
effective config, final dispatch hooks, included entitlement/billing policy,
launched SDK identity and harness-build linkage are explicit. It is a partial
measurement library, not a complete guard producer or an ordinary-service unlock.

Current external boundary remains the observer README's core final-dispatch and
entitlement contract: truthful post-final-hook/connection/billing fields require
authorized OpenCode core observability or separately measured human handoff, not
Tessera manifest echoes. Production wiring/owned handles/approved artifact handoff
and real installed evidence are still required. Local fixture proofs use only an
innocuous locally compiled sleep fixture and temporary nonsecret build/SDK bytes.

Scoped acceptance (2026-10-06): approved existing isolated environment ran
`python -m pytest tests/test_research_identity.py -q`: **17 passed in 8.96s,
exit 0, no warnings or skips**. Scoped Ruff lint/format and whitespace checks passed.
Actual owned process/native metadata/hash/version reads are local executable proof;
start-change and foreign-UID conditions use isolated injected negative metadata.
Tests cover exit/handle change, changed executable/build/metadata, symlink/parent
symlink, writable/hardlinked/unowned/escaping paths and disallowed source/secret
references. The initial copied macOS system executable exited with SIGKILL; no
security settings/signatures were changed. The final fixture is a tiny locally
compiled C program, always terminated/reaped, with no service/harness/model launch.
No existing auth data, inference, install, host access or full suite was used.
