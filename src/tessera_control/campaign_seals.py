"""Role-sealed campaign inputs and artifacts (ADR 0164).

A v2 spec may list ``seals``: each names a pinned input digest (a ``snapshot`` digest or an
``artifact`` sha256, wherever a stage mounts it) or one output of a stage in the spec, and the
role it is sealed for (for example ``evaluation``). A stage that mounts sealed data, directly or
through a stage output derived from it, must declare that role in its ``roles``; otherwise the
spec is refused when it is parsed, so it can never be stored, approved or run. A derived output
inherits every seal its producing stage read, so the guarantee follows the lineage inside the
spec rather than one hop. What each stage reads under a seal is recorded on its receipt entry.

The functions here are pure over the normal form (``NormalSpec``); they read only ``seals``,
``stages``, ``roles``, ``inputs`` and ``outputs``. A spec without seals has nothing to check
and no receipt section, so every existing spec and receipt keeps its shape and digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The placeholder digest a not-yet-resolved ``receipt_output`` reference carries during the
# structure check of a submit request (ADR 0080); submit re-validates the resolved spec.
_UNRESOLVED_DIGEST = "0" * 64


@dataclass(frozen=True)
class SealedRead:
    """One sealed thing a stage mounts: ``via`` is ``direct`` or the upstream stage key."""

    seal: str
    role: str
    input: str
    via: str

    def as_receipt(self) -> dict[str, str]:
        return {
            "seal": self.seal,
            "role": self.role,
            "input": self.input,
            "via": "direct" if self.via == "direct" else "lineage",
            **({} if self.via == "direct" else {"from_stage": self.via}),
        }


def _input_digest(item: Any) -> str | None:
    if item.kind == "snapshot":
        return item.digest
    if item.kind == "artifact":
        return item.sha256
    return None


def _stage_reads(spec: Any) -> dict[str, list[SealedRead]]:
    """Every sealed read of every stage, following lineage through in-spec stage outputs.

    A stage that reads any seal taints all of its outputs with those seals (the platform cannot
    tell which output a stage derived from which input). Computed to a fixed point, so the
    result does not depend on stage order; the taint sets only grow and are bounded by
    stages x seals, so the loop always ends."""
    seals = tuple(getattr(spec, "seals", ()) or ())
    stages = {stage.key: stage for stage in spec.stages}
    by_digest: dict[str, list[Any]] = {}
    by_output: dict[tuple[str, str], list[Any]] = {}
    for seal in seals:
        if seal.digest is not None:
            by_digest.setdefault(seal.digest, []).append(seal)
        else:
            by_output.setdefault((seal.stage, seal.output), []).append(seal)
    paths = {
        key: {output.path: output.name for output in stage.outputs} for key, stage in stages.items()
    }
    tainted: dict[str, set[tuple[str, str]]] = {key: set() for key in stages}
    while True:
        reads: dict[str, list[SealedRead]] = {}
        for stage in spec.stages:
            found: list[SealedRead] = []
            for item in stage.inputs:
                digest = _input_digest(item)
                for seal in by_digest.get(digest or "", ()):
                    found.append(SealedRead(seal.name, seal.role, item.name, "direct"))
                if item.kind != "stage" or item.stage not in stages:
                    continue
                names = {paths[item.stage].get(path) for path in item.paths}
                for name in sorted(n for n in names if n is not None):
                    for seal in by_output.get((item.stage, name), ()):
                        found.append(SealedRead(seal.name, seal.role, item.name, "direct"))
                for seal_name, role in sorted(tainted[item.stage]):
                    found.append(SealedRead(seal_name, role, item.name, item.stage))
            reads[stage.key] = found
        updated = {key: tainted[key] | {(r.seal, r.role) for r in reads[key]} for key in stages}
        if updated == tainted:
            return reads
        tainted = updated


def validate_seals(spec: Any) -> None:
    """Raise ValueError when a seal is malformed or a stage reads sealed data without its role.

    Called by ``validate_structure``, so it runs whenever a spec is parsed: at submit, at
    approval and every time the runner reads the stored spec."""
    seals = tuple(getattr(spec, "seals", ()) or ())
    for stage in spec.stages:
        roles = tuple(getattr(stage, "roles", ()) or ())
        if len(set(roles)) != len(roles):
            raise ValueError(f"Stage {stage.key} roles must be distinct")
    if not seals:
        return
    names = [seal.name for seal in seals]
    if len(set(names)) != len(names):
        raise ValueError("Seal names must be unique")
    stages = {stage.key: stage for stage in spec.stages}
    pinned = {_input_digest(item) for stage in spec.stages for item in stage.inputs} - {None}
    unresolved = _UNRESOLVED_DIGEST in pinned
    targets: set[tuple[str, ...]] = set()
    for seal in seals:
        if seal.digest is not None:
            target: tuple[str, ...] = (seal.role, seal.digest)
            # Refuse a seal that protects nothing: a mistyped digest must not look sealed.
            if seal.digest not in pinned and not unresolved:
                raise ValueError(
                    f"Seal {seal.name!r}: digest matches no snapshot or artifact input of the spec"
                )
        else:
            target = (seal.role, seal.stage, seal.output)
            producer = stages.get(seal.stage)
            if producer is None:
                raise ValueError(f"Seal {seal.name!r} names unknown stage {seal.stage}")
            if seal.output not in {output.name for output in producer.outputs}:
                raise ValueError(
                    f"Seal {seal.name!r} names undeclared output {seal.output} of {seal.stage}"
                )
        if target in targets:
            raise ValueError(f"Seal {seal.name!r}: the same data is sealed twice for one role")
        targets.add(target)
    reads = _stage_reads(spec)
    for stage in spec.stages:
        roles = set(getattr(stage, "roles", ()) or ())
        for read in reads[stage.key]:
            if read.role in roles:
                continue
            how = "" if read.via == "direct" else f" (derived through stage {read.via})"
            raise ValueError(
                f"Stage {stage.key}: input {read.input!r} mounts sealed artifact "
                f"{read.seal!r}{how}, sealed for role {read.role!r}; only a stage that "
                f"declares roles [{read.role!r}] may read it"
            )


def sealed_access(spec: Any, stage_key: str) -> list[dict[str, str]]:
    """The receipt record of what a stage reads under a seal: seal, role, input and how
    (``direct``, or ``lineage`` with ``from_stage``). Empty for a spec without seals."""
    if not getattr(spec, "seals", ()):
        return []
    reads = _stage_reads(spec).get(stage_key, [])
    unique = {(r.seal, r.role, r.input, r.via): r for r in reads}
    return [unique[key].as_receipt() for key in sorted(unique)]


def carried_seals(spec: Any, rerun: set[str], reused: list[dict[str, str]]) -> list[dict[str, str]]:
    """The seals of a re-run spec (ADR 0069) planned from the original ``spec`` (normal form).

    A stage that is not re-run disappears and its outputs come back as pinned ``artifact``
    inputs (``reused``: stage, output, sha256). Each such artifact keeps every seal it carried
    in the original spec, as a digest seal on its sha256: the seal on the output itself, and
    every seal its producing stage read (lineage). A digest seal stays while a re-run stage still
    mounts that digest, and an output seal stays while its stage is re-run. Nothing is
    dropped that a re-run stage could still reach, so a re-run never reads more than the
    original was allowed to."""
    reads = _stage_reads(spec)
    taint = {key: {(r.seal, r.role) for r in found} for key, found in reads.items()}
    kept_digests = {
        _input_digest(item) for stage in spec.stages if stage.key in rerun for item in stage.inputs
    } - {None}
    carried: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(entry: dict[str, str], target: tuple[str, ...]) -> None:
        if target not in seen:
            seen.add(target)
            carried.append(entry)

    for seal in getattr(spec, "seals", ()) or ():
        if seal.digest is not None:
            if seal.digest in kept_digests:
                entry = {"name": seal.name, "role": seal.role, "digest": seal.digest}
                add(entry, (seal.role, seal.digest))
        elif seal.stage in rerun:
            entry = {"name": seal.name, "role": seal.role, "stage": seal.stage}
            entry["output"] = seal.output
            add(entry, (seal.role, seal.stage, seal.output))
    for item in reused:
        on_output = {
            (seal.name, seal.role)
            for seal in getattr(spec, "seals", ()) or ()
            if seal.digest is None and (seal.stage, seal.output) == (item["stage"], item["output"])
        }
        for seal_name, role in sorted(on_output | taint.get(item["stage"], set())):
            entry = {
                "name": f"{seal_name[:23]}-{item['sha256'][:8]}",
                "role": role,
                "digest": item["sha256"],
            }
            add(entry, (role, item["sha256"]))
    return carried
