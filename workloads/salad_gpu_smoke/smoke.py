"""Fixed public CUDA smoke operation; stdlib custody and bounded supervisor.

Salad IMDS supplies workload identity; external custody verifies and authorizes it.
Unavailable identity or uncertain custody admission fails closed before CUDA.
"""

import base64
import hashlib
import http.client
import json
import multiprocessing
import os
import re
import signal
import ssl
import sys
import time
from dataclasses import asdict, dataclass


class Failure(Exception):
    """An intentionally detail-free workload failure."""


@dataclass(frozen=True)
class Request:
    version: str
    public_input: str
    operation: str
    epoch: str
    destination: str
    watchdog: str
    max_seconds: int
    max_output_bytes: int
    max_output_files: int
    dimension: int

    @classmethod
    def parse(cls, argv):
        if len(argv) != 10 or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v) for v in argv
        ):
            raise Failure()
        if argv[:2] != ["v1", "public-integers-v1"]:
            raise Failure()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", argv[2]):
            raise Failure()
        if not re.fullmatch(r"[1-9][0-9]{0,8}", argv[3]):
            raise Failure()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", argv[4]):
            raise Failure()
        if any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in argv[4].split(".")
        ):
            raise Failure()
        if not re.fullmatch(r"[a-f0-9]{64}", argv[5]):
            raise Failure()
        if any(not re.fullmatch(r"[1-9][0-9]{0,7}", v) for v in argv[6:]):
            raise Failure()
        seconds, output_bytes, files, dimension = map(int, argv[6:])
        if not (
            1 <= seconds <= 300
            and 256 <= output_bytes <= 65536
            and files == 1
            and 2 <= dimension <= 32
        ):
            raise Failure()
        return cls(*argv[:6], seconds, output_bytes, files, dimension)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def compute(request, torch):
    """Only generated public integers; no file, model or network inputs."""
    if not torch.cuda.is_available():
        raise Failure()
    n = request.dimension
    # Float64 CUDA matmul is supported; all these small integers/sums are exact
    # below 2**53. CUDA integer matmul is not supported by every torch build.
    values = torch.arange(n * n, dtype=torch.float64, device="cuda").reshape(n, n)
    if values.device.type != "cuda":
        raise Failure()
    result = values @ values.T
    if values.device.type != "cuda" or result.device.type != "cuda":
        raise Failure()
    torch.cuda.synchronize()
    total = int(result.sum().item())
    expected = sum(
        sum((i * n + k) * (j * n + k) for k in range(n)) for i in range(n) for j in range(n)
    )
    if total != expected:
        raise Failure()
    return encode(
        {
            "schema": "tessera.cuda-smoke.v1",
            "operation": request.operation,
            "epoch": request.epoch,
            "public_input": request.public_input,
            "dimension": n,
            "sum": total,
            "cuda": {
                "available": True,
                "input_device": "cuda",
                "result_device": "cuda",
                "synchronized": True,
            },
        }
    )


def child(request, pipe):
    try:
        # Native library/import diagnostics are unbounded and may contain details.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        import torch

        output = compute(request, torch)
        if len(output) > request.max_output_bytes:
            raise Failure()
        pipe.send_bytes(output)
    except BaseException:
        # No raw exception text, traceback, provider response, or CUDA detail.
        pipe.send_bytes(b"")
    finally:
        pipe.close()


def bounded_compute(request, deadline):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=child, args=(request, sender))
    process.start()
    sender.close()
    try:
        if not receiver.poll(max(0, deadline - time.monotonic())):
            raise Failure()
        output = receiver.recv_bytes(request.max_output_bytes)
        if not output:
            raise Failure()
        return output
    except (OSError, EOFError):
        raise Failure() from None
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join()
        process.close()


class HTTPSCustody:
    """No unauthenticated fallback, redirects, management key, or env lookup."""

    def __init__(self, request, deadline):
        self.request = request
        self.deadline = deadline
        self.context = ssl.create_default_context()
        self.token, self.token_expires = self.identity()

    def identity(self):
        """Fixed link-local IMDS, direct socket, no proxy, redirect, or retry."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Failure()
        conn = http.client.HTTPConnection("169.254.169.254", 80, timeout=min(5, remaining))
        try:
            conn.request("GET", "/v1/token", headers={"Metadata": "true"})
            response = conn.getresponse()
            if response.status != 200:
                raise Failure()
            raw = response.read(4097)
            if len(raw) > 4096 or time.monotonic() >= self.deadline:
                raise Failure()
            body = json.loads(raw)
            token = body.get("jwt") if isinstance(body, dict) else None
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 1000
                or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
            ):
                raise Failure()
            payload = token.split(".")[1]
            claims = json.loads(
                base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            )
            expiry = claims.get("exp") if isinstance(claims, dict) else None
            # Unverified expiry is ONLY a refresh hint. Server verifies all claims.
            if type(expiry) is not int or expiry <= time.time():
                raise Failure()
            return token, expiry
        except Exception:
            raise Failure() from None
        finally:
            conn.close()

    def post(self, path, body):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Failure()
        conn = http.client.HTTPSConnection(
            self.request.destination, 443, timeout=min(10, remaining), context=self.context
        )
        try:
            conn.request(
                "POST",
                path,
                body=encode(body),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.token,
                },
            )
            response = conn.getresponse()
            if response.status != 200:
                raise Failure()
            raw = response.read(4097)
            if len(raw) > 4096 or time.monotonic() >= self.deadline:
                raise Failure()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise Failure()
            return result
        except Exception:
            raise Failure() from None
        finally:
            conn.close()

    def begin_once(self, request):
        result = self.post("/v1/workload/begin-once", {"binding": asdict(request)})
        return result == {
            "schema": "tessera.begin-once.v1",
            "granted": True,
            "operation": request.operation,
            "epoch": request.epoch,
        }

    def retain(self, request, output, digest):
        # At most one proactive refresh, solely for upload, never claim/compute retry.
        if time.time() >= self.token_expires - 15:
            self.token, self.token_expires = self.identity()
        result = self.post(
            "/v1/workload/output",
            {"binding": asdict(request), "sha256": digest, "output": json.loads(output)},
        )
        return result == {
            "schema": "tessera.retained.v1",
            "sha256": digest,
            "operation": request.operation,
            "epoch": request.epoch,
            "retained": True,
        }


def execute(request, custody, deadline, runner=bounded_compute):
    # An uncertain/lost/rejected claim is terminal. Never retry or release a claim.
    if time.monotonic() >= deadline or custody.begin_once(request) is not True:
        raise Failure()
    if time.monotonic() >= deadline:
        raise Failure()
    output = runner(request, deadline)
    if (
        not isinstance(output, bytes)
        or not output
        or len(output) > request.max_output_bytes
        or time.monotonic() >= deadline
    ):
        raise Failure()
    digest = hashlib.sha256(output).hexdigest()
    if custody.retain(request, output, digest) is not True or time.monotonic() >= deadline:
        raise Failure()
    return digest


def main(argv):
    armed = False
    try:
        request = Request.parse(argv)

        # Linux image only. Covers bootstrap, trickling HTTPS, compute and upload.
        def expired(signum, frame):
            raise Failure()

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, request.max_seconds)
        armed = True
        deadline = time.monotonic() + request.max_seconds
        digest = execute(request, HTTPSCustody(request, deadline), deadline)
        print(
            encode(
                {"schema": "tessera.smoke-status.v1", "status": "retained", "sha256": digest}
            ).decode()
        )
        return 0
    except BaseException:
        print('{"schema":"tessera.smoke-status.v1","status":"failed"}')
        return 1
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
