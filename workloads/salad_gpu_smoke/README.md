# Bounded Salad CUDA smoke recipe

This is an **unbuilt, undeployed recipe**, not GPU or live custody evidence.
It uses only preinstalled torch and Python's standard library. No install,
download, external data, shell, queue, gateway, or management credential is used.

## Image and approved entry

Build context is this directory. An owner must explicitly supply
`TORCH_CUDA_BASE=registry/repository@sha256:<64 lowercase hex>` naming a reviewed
linux/amd64 image with Python 3, CUDA-enabled torch, CA support and a readable
Python installation for UID 65532. The Dockerfile validates the digest syntax,
architecture and CUDA-enabled torch build; a tag-only base fails. Docker resolves
the FROM before its RUN validation, so validate the argument before any build or
pull. No default base is provided and no build/pull is authorized by this recipe.
Publish and retain the resulting immutable image digest before profile admission.

The fixed `/tessera/bin/run` takes exactly these ten non-secret approved tokens:

```text
v1 public-integers-v1 OPERATION EPOCH CUSTODY_DNS WATCHDOG_SHA256 MAX_SECONDS MAX_OUTPUT_BYTES 1 DIMENSION
```

Operation is a lowercase name; epoch is a positive decimal string; DNS is an
exact reviewed HTTPS service (port 443, no URL or credentials). Watchdog is the
approved 64-hex profile digest. Limits: 1–300 seconds, 256–65536 output bytes,
exactly one file, dimension 2–32. No arbitrary code/input path is accepted.
Set only the existing non-secret `PYTHONUNBUFFERED=1` and `OMP_NUM_THREADS=1`
when required by the profile. The watchdog digest identifies external policy;
the workload cannot prove independent enforcement of that policy.

The generated public integer matrix is multiplied by its transpose on CUDA,
synchronized, and checked against a deterministic integer reference. CUDA absent
or any non-CUDA tensor fails; there is no CPU fallback. One bounded canonical JSON
artifact reports typed CUDA assertions and the checked sum. Its SHA-256 covers
the exact canonical bytes. This is workload-reported proof, not GPU attestation.
No raw exception/traceback or provider response is printed.

## Native identity and authenticated custody interface

Salad provides a native bootstrap that requires no secret injection. The client
directly calls fixed `http://169.254.169.254:80/v1/token` with `Metadata: true`,
before begin-once, using stdlib sockets rather than proxy-aware transports.
See the [IMDS token API](https://docs.salad.com/reference/imds/get-token) and
[JWT authentication contract](https://docs.salad.com/container-engine/tutorials/security/jwt-authentication).
It accepts only status 200, bounds the response to 4096 bytes and `jwt` to 1000
characters, requires compact JWT syntax and a future integer expiry, and never
follows redirects or retries. Missing/malformed identity fails before compute.

Only the approved destination receives the identity, through an `Authorization:
Bearer <JWT>` header on HTTPS port 443 with default system CA/hostname verification.
No token goes into argv, environment, output, request bodies or diagnostics; no
management key, client certificate, custom CA or embedded private key is needed.
The unverified token expiry is only a refresh scheduling hint, never authority.
If within 15 seconds of expiry before upload, retrieve identity once more within
the original deadline. Never retry a failed claim, computation or upload. The
external server must independently verify ES256 signature via Salad's fixed JWKS,
issuer, audience, expiry/not-before, organization, group and machine claims, then
bind that identity to server-owned exact approval records. Operation IDs alone
never grant writer authority. Provider identity is not GPU/scientific attestation.

**Deployment prerequisite:** an actual independently deployed HTTPS custody
service with correctly configured Salad claim verification, durable operation-wide
claims, output custody and independent guardian is still required. The recipe
does not configure or deploy it; local tests cannot prove those live boundaries.

The protocol retains its original binding/JSON bodies. It uses POST JSON, response
size <=4096 bytes, status 200 only, no redirects, native JWT authentication, and:

* `/v1/workload/begin-once`: `{"binding": <all Request fields>}`. The server
  authenticates workload identity and binds it to the tenant, exact approved
  operation, epoch, destination, watchdog, public input, limits and image digest
  stored server-side. It must atomically consume a durable **operation-wide**
  execution claim before returning exactly
  `{"schema":"tessera.begin-once.v1","granted":true,"operation":OPERATION,"epoch":EPOCH}`.
  An epoch change/reallocation must never reset the consumed claim. Any consumed,
  rejected, lost-response, or uncertain claim is terminal; no GPU work is retried,
  including a crash before upload. Writer authority never comes from body fields.
* `/v1/workload/output`: `{"binding": <same fields>, "sha256": HASH,
  "output": <artifact object>}`. The server verifies the authenticated owner,
  consumed claim, limits, one-artifact rule, CUDA schema, canonical-byte hash and
  durable quarantined retention before returning exactly
  `{"schema":"tessera.retained.v1","retained":true,"sha256":HASH,"operation":OPERATION,"epoch":EPOCH}`.
  Durable acknowledgment does not promote output to trusted evidence.

Begin-once precedes torch import and subprocess creation. The client caps responses,
wall time, and child output; the Linux alarm also bounds trickling HTTP traffic.
The compute child is killed and joined on failure/expiry. Local exit is not provider
stop proof: an independent guardian must stop/reconcile the exact provider group,
retain output, account for exposure, and confirm termination. Normal reconnect
reads the original retained record; it never clears the consumed claim. Bootstrap
identity expiry must not disable trusted-server retrieval or stop-only recovery.

## Local tests and later live acceptance

`tests/test_salad_gpu_smoke.py` uses fake torch, custody, IMDS and HTTPS transports
to test admission ordering, bounds, token redaction and unavailable identity.
It does not prove a GPU, image build, actual IMDS, live JWT-verifying custody
server, custody durability, or provider stop. The parent owns live smoke harness,
approved financial scope and independent monitor setup; this recipe alone must
not launch paid work.
