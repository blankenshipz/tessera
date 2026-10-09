# Bounded tabular starter

This is a deterministic engineering example, not scientific validation. No Ray,
dataframe framework, model provider, download or automatic approval is needed.
Actual numeric analysis uses the standard library; immutable snapshot verification
reuses Tessera's existing typed verifier dependencies.

## Preregister and approve before running

Generate `tessera_starter.synthetic.example()` once: seed1729,120 immutable IDs,
train80/validation20/test20. Keep the actual CSV bytes; do not resplit or regenerate
between trials. Source/license are declarations, not verified rights or origin.
Have the human review data hash/size, schema and canonical manifest digest, then
upload through `POST /projects/{id}/tabular-dataset-snapshots`. Human snapshot
release is separate from adoption and must identify the bounded worker principal,
expiry and exact input snapshot digest.

Register **all four** variants before any approval: `constant` (training target
mean), `linear` (training-only first numeric feature OLS), `half-slope` (same fit,
preregistered coefficient intervention), `zero-slope` (explicit zero coefficient
control). OLS singular variance falls back truthfully to the constant train mean.
No validation/test values train the model. This tiny sweep has no adaptive search,
ranking-driven retries or additional trial creation.

Each normal task plan binds the same snapshot, immutable image, question, metrics,
variant and output path; normal experiment design/admission and human task approval
precede the human-issued execution grant. Use max concurrency1, <=4 preregistered
trials, <=120seconds/trial, <=1CPU, <=512MiB, <=4MiB CSV, <=1MiB output, network false.
The starter/native descriptor is not a permission and cannot approve itself.

The explicit-role `examples/tabular-starter/workflow.py` calls normal upload,
plan/grant/status/revoke and worker input routes using an environment credential
reference. It cannot approve or release snapshots. Run human commands manually,
separately from the worker's token/session. Response files are private and may
contain input bytes; never upload them as logs. Do not use a human token for worker
commands. Payloads must come from actual current API records, not guessed revisions.
Cancellation acknowledgement is not confirmed termination; retain actual worker
termination and exposure/reconciliation receipts.

## Workload command and custody

Build `examples/tabular-starter/Dockerfile` from repo root. Its public amd64 Python
base is digest-pinned, observed2026-10-07. Build-time typed verifier dependencies
are version/hash-pinned in `requirements.lock`; runtime makes no installs/downloads.
The workload build slices actual pure verifier definitions verbatim out of the
canonical backend module, excluding only Store/API imports and its Store class.
This avoids shipping/importing control services in an experiment image; the image
retains canonical/sliced source hashes and targeted parity checks exercise the same
loader. User65532, read-only root, no
capabilities/network, and container/Job resource limits remain mandatory. A local
image tag or image-config digest is **not** a workload registry-manifest pin.
Use the exact OCI manifest digest from the retained built artifact; installed target
availability is separate and must be verified before ordinary dispatch.

### Portable operator build

[`IMAGE.lock.json`](../examples/tabular-starter/IMAGE.lock.json) is an audit record
of an **observed local build, not a published image**. Its measured workload
manifest digest, archive SHA256/size and observed base pin remain historical
evidence. The archive is not bundled with the source/wheel, and the record does
not guarantee any registry serves that workload digest. No machine-local archive
path is needed by the public workflow.

From the repository root, choose an operator-owned output directory and an
explicit, already authorized Docker context. These variables are build artifact
references, not runtime credentials or execution authority:

```sh
export TESSERA_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tessera-tabular-build.XXXXXX")"
export TESSERA_BUILD_CONTEXT=desktop-linux # replace with your authorized context
export TESSERA_STARTER_OCI="$TESSERA_BUILD_DIR/starter.oci.tar"
docker --context "$TESSERA_BUILD_CONTEXT" buildx build \
  --platform linux/amd64 --provenance=false \
  -f examples/tabular-starter/Dockerfile \
  --output "type=oci,dest=$TESSERA_STARTER_OCI" .
```

This recipe uses the pinned base and hash-locked build dependencies; it can fetch
them at build time. It does not publish or authorize a workload. Bit-identical
image reproduction across toolchain versions has **not** been established.
Record your actual artifact digest, not the historical digest copied from the
lock. For this single-platform, no-attestation build, inspect verified OCI blobs:

```sh
python3 - <<'PY'
import hashlib, json, os, tarfile
from pathlib import Path

archive = Path(os.environ["TESSERA_STARTER_OCI"])
with tarfile.open(archive) as oci:
    def blob(descriptor):
        algorithm, digest = descriptor["digest"].split(":", 1)
        assert algorithm == "sha256"
        data = oci.extractfile("blobs/sha256/" + digest).read()
        assert len(data) == descriptor["size"]
        assert hashlib.sha256(data).hexdigest() == digest
        return json.loads(data)

    index = json.load(oci.extractfile("index.json"))
    for _ in range(8):
        assert len(index["manifests"]) == 1, "Inspect multi-manifest builds explicitly"
        descriptor = index["manifests"][0]
        manifest = blob(descriptor)
        if "config" in manifest:
            break
        index = manifest
    else:
        raise ValueError("OCI index nesting exceeds bound")
    config = blob(manifest["config"])
    assert (config["os"], config["architecture"]) == ("linux", "amd64")
    print("workload_manifest_digest:", descriptor["digest"])
print("oci_archive_sha256:", hashlib.file_digest(archive.open("rb"), "sha256").hexdigest())
print("oci_archive_size:", archive.stat().st_size)
PY
```

Keep this output and your source/toolchain/build receipts with the operator-owned
archive. The human must review and bind the **actual** resulting manifest pin
before plan approval and grant issuance. Verify exact-manifest availability on
the approved execution target separately; do not substitute a local tag, image
config ID or unpublished registry reference. A changed image requires the normal
new plan review/approval and grant flow; neither a caller profile nor this recipe
automatically changes an existing plan or grant. Genuine owner/runtime binding
proof remains a separate prerequisite, not a local-build dependency.

Normal approved argv (output path is approved PVC mount or `/work`):

```text
python -m tessera_starter --snapshot-root /dataset --manifest-digest <approved-manifest-sha256> --variant linear --output-root /output
```

The native `dataset_input` descriptor is derived from the canonical package's
existing approved snapshot digest, approved manifest argv and **actual current
lease-delivered bytes**. No extra control package field or authority widening is
needed. `KubernetesExecutionPackage.from_admitted_package(..., input_bundle=...,
dataset_namespace=...)` verifies this binding and freezes the descriptor/resources
into the native Job view. It pins snapshot/manifest and ordered chunk hashes/sizes. The
worker obtains **actual released bytes** through the normal lease input endpoint,
checks fresh current lease/epoch/grant/release before each side effect, derives the
same descriptor and creates immutable operation-owned ConfigMaps. Raw640KiB chunks
(base64~854KiB plus small metadata below1MiB) cover4MiB in at most seven chunks plus
one manifest; no truncation. UID receipts must be durable before further effects.
Init runs the exact workload image, verifies every chunk, copies into a bounded
emptyDir and rechecks the complete manifest/schema/splits. Computation gets only a
read-only `/dataset` mount and consumes exact verified bytes, never reopens CSV.
No hostPath, Secret, arbitrary server path or new ambient cluster permission.
Cleanup requires matching namespace/name/operation/project/grant custody and exact
UID delete. Ordinary dispatch now wires authenticated current-input callbacks and
durable ConfigMap custody into scoped native clients. All names/spec hashes are
journaled before effects; each actual UID is persisted before the next create.
Lost replies permit only exact planned-resource adoption under fresh authority;
an uncertain absent create is inspect-only, never blindly retried or replaced.
Retained input bytes reconstruct observation/cancellation identity, not authority.

Network-none still requires the existing fresh target-bound real allowed/denied
connectivity evidence. A NetworkPolicy declaration alone does not establish it.
The Job uses backoffLimit0 and no workload autoretry. Killing the whole process may
prevent a final file: the supervisor must record the actual terminal outcome and
never synthesize success or zero exposure from missing results.

## Outputs and review

`result.json` contains finite MSE/MAE per unchanged split, observed data/manifest
and analysis-code hashes, parameter digest, split hashes/counts, source/license
declarations and Python/platform facts. Success remains quarantined and scientific
validation explicitly not-established. Exceptions yield an inconclusive/cancelled
manifest with failure category and **no metrics**. Logs are bounded status lines,
not raw data/prompts. Existing output quarantine, manifest/artifact custody,
notebook provenance, separately authenticated human review and reviewed export
remain the normal workflow; this CLI neither promotes nor exports evidence.

## Current verification boundary

Real local tests exercised four preregistered plans before approval, actual normal
ASGI/auth/SQLite adoption/release/grant/lease inputs, the actual loaded OCI image
for four sequential computations, actual quarantine/project placement/manifest,
human disposition and custody release followed by byte-exact content export.
Temporary test human/worker identities are synthetic; this is not production human
identity or deployed production services.
`IMAGE.lock.json` identifies the exact tested local OCI manifest (not published).
A separate bounded local HTTPS/control/ordinary-worker run observed actual allowed
and denied network probes, imported that exact OCI manifest into Docker Desktop's
node containerd, and completed one native baseline Job with verified init inputs.
The service committed a quarantined result; human review/export and admitted
experiment artifact/measurement links were exercised against the same durable state.
Four tasks were preregistered before approval as two paired experiment records (the
normal experiment API supports two variants per record). No native four-trial sweep,
physical UI, off-host reasoning or scientific validation is claimed. That earlier
native starter run is indexed as local evidence ID
`TABULAR-STARTER-NATIVE-BASELINE-INTENT8`; it predates the later candidate-bound
restart/cancellation slice summarized in the
[release evidence note](release-evidence-intent9.md). Never infer native execution
from a dictionary manifest, CPU CLI or Docker mount.

The earlier native starter run used an implementation preceding conservative
uncertain-create hardening and does not verify that historical source revision.
The later `0.1.0rc2` candidate-bound native slice is summarized separately in the
[release evidence note](release-evidence-intent9.md); its temporary identities and
synthetic dataset do not prove
production human identity, scientific validity, generated reasoning or independent-
host execution. The older run's initial harness failure and review-resume details
remain under the same local evidence ID; the Job was not resubmitted just to repair
the review harness.
