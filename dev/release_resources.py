"""Closed starter resource contract; no reads of an actively authored workload."""

STARTER_FILES = frozenset(
    {
        "Dockerfile",
        "Dockerfile.dockerignore",
        "IMAGE.lock.json",
        "portable_verifier.py",
        "requirements.lock",
        "workflow.py",
    }
)
STARTER_SOURCE = "examples/tabular-starter"
STARTER_INSTALLED = "share/tessera/examples/tabular-starter"


def starter_resource_map(version: str) -> dict[str, str]:
    prefix = f"tessera_control-{version}.data/data/{STARTER_INSTALLED}"
    return {f"{STARTER_SOURCE}/{name}": f"{prefix}/{name}" for name in sorted(STARTER_FILES)}
