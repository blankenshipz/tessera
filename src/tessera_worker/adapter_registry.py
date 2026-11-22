"""Registry of execution adapter factories keyed by ``TargetBinding.adapter_kind``.

Factories are documented constructors (they take the worker-owned dependencies the
adapter needs); registration confers no authority and activates nothing.
"""

from collections.abc import Callable

_FACTORIES: dict[str, Callable] = {}


def register_adapter(kind: str, factory: Callable) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError("adapter kind must be a non-empty string")
    if not callable(factory):
        raise ValueError("adapter factory must be callable")
    if kind in _FACTORIES:
        raise ValueError(f"Adapter kind already registered: {kind}")
    _FACTORIES[kind] = factory


def adapter_kinds() -> tuple[str, ...]:
    return tuple(_FACTORIES)


def adapter_factory(kind: str) -> Callable:
    try:
        return _FACTORIES[kind]
    except (KeyError, TypeError):
        raise KeyError(f"Unknown adapter kind: {kind!r}") from None


def _kubernetes(*args, **kwargs):
    """``KubernetesExecutionProtocolAdapter(KubernetesExecutionAdapter)``."""
    from .kubernetes_execution import KubernetesExecutionProtocolAdapter

    return KubernetesExecutionProtocolAdapter(*args, **kwargs)


def _docker(*args, **kwargs):
    """``OrdinaryDockerExecutionAdapter(journal, target, transport, *, current_authority)``."""
    from .ordinary_docker_execution import OrdinaryDockerExecutionAdapter

    return OrdinaryDockerExecutionAdapter(*args, **kwargs)


register_adapter("kubernetes-job", _kubernetes)
register_adapter("docker-container-v1", _docker)
