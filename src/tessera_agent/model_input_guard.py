"""Refuse model inputs whose digest is a sealed holdout partition (ADR 0068).

The caller supplies ``lookup(digest)``, the control API body of
``GET /projects/{project_id}/holdout-claims?partition_digest=<digest>``. Any claim means the
partition is sealed. Anything unverifiable fails closed. Errors carry only a fixed code, never
a digest or a lookup body.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

_HEX = frozenset("0123456789abcdef")


class SealedInputRefused(ValueError):
    """str() is the sanitized code only."""


def _valid(digest: Any) -> bool:
    return isinstance(digest, str) and len(digest) == 64 and _HEX.issuperset(digest)


def refuse_sealed_inputs(digests: Iterable[str], lookup: Callable[[str], dict]) -> None:
    unique = list(dict.fromkeys(digests))
    if not unique:
        return
    if not all(_valid(d) for d in unique):
        raise SealedInputRefused("model_input_digest_invalid")
    sealed = False
    for digest in unique:
        try:
            body = lookup(digest)
            claims = body["claims"]
            truncated = body["truncated"]
            if not isinstance(claims, list) or not isinstance(truncated, bool):
                raise TypeError
        except Exception:
            raise SealedInputRefused("model_input_seal_unknown") from None
        if claims:
            sealed = True
        elif truncated:
            raise SealedInputRefused("model_input_seal_unknown")
    if sealed:
        raise SealedInputRefused("model_input_sealed")
