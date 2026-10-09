"""Isolated namespace pagination regressions; no cluster effects."""

from types import SimpleNamespace

import pytest

from tessera_worker.kubernetes_pod_listing import list_owned_pods


def pod(uid="pod-1", name="owned", owner="job-1"):
    return {
        "metadata": {"uid": uid, "name": name, "ownerReferences": [{"kind": "Job", "uid": owner}]}
    }


def page(items, token="", version="1"):
    return {
        "apiVersion": "v1",
        "kind": "PodList",
        "metadata": {"continue": token, "resourceVersion": version},
        "items": items,
    }


def client(pages):
    calls = []

    def request(method, path):
        calls.append((method, path))
        return pages[len(calls) - 1]

    return SimpleNamespace(request=request, path=lambda _: "/pods", calls=calls)


def test_namespace_noise_does_not_hide_owned_pod_on_later_page():
    connection = client(
        [page([pod(owner="other")] * 100, "a+/="), page([None, {"metadata": []}, pod()])]
    )
    assert list_owned_pods(connection, "job-1") == [pod() | {"apiVersion": "v1", "kind": "Pod"}]
    assert connection.calls == [
        ("GET", "/pods?limit=100"),
        ("GET", "/pods?limit=100&continue=a%2B%2F%3D"),
    ]


@pytest.mark.parametrize(
    "pages, message",
    [
        ([page([], "a"), page([], "a")], "token was replayed"),
        ([page([pod()], "a"), page([pod()])], "identity was replayed"),
        ([page([pod()], "a"), page([pod(uid="replacement")])], "identity was replayed"),
        ([page([pod()], "a"), page([], version="2")], "snapshot changed"),
        ([page([pod(uid="")])], "identity is missing"),
        ([page([pod() | {"kind": "Job"}])], "invalid resource type"),
        ([page([], 42)], "token is invalid"),
        ([page([pod()] * 101)], "bounded typed page"),
    ],
)
def test_replay_replacement_and_invalid_pages_fail_closed(pages, message):
    with pytest.raises(ValueError, match=message):
        list_owned_pods(client(pages), "job-1")


def test_continuation_bound_is_enforced_even_for_unrelated_pods():
    connection = client([page([], "a"), page([], "b")])
    with pytest.raises(ValueError, match="continuation limit"):
        list_owned_pods(connection, "job-1", max_pages=2)
    assert len(connection.calls) == 2


@pytest.mark.parametrize("replacement", [False, True])
def test_output_collector_uses_all_pages_and_checks_exact_handoff_uids(monkeypatch, replacement):
    from tessera_worker import kubernetes_output_collector as module

    cluster = SimpleNamespace(namespace="work", namespace_uid="namespace-uid")
    connection = client(
        [
            page([pod(owner="other")] * 100, "next"),
            page(
                [
                    pod(uid="replacement" if replacement else "pod-1")
                    | {"status": {"phase": "Succeeded"}}
                ]
            ),
        ]
    )
    connection.cluster = cluster
    connection.get = lambda *_: {"metadata": {"uid": "job-1"}}
    connection.verify = lambda *_: None
    output = SimpleNamespace(
        pvc_uid="pvc-uid", pvc_name="outputs", mount_path="/outputs", pvc_spec_sha256="spec"
    )
    package = SimpleNamespace(
        operation_id="operation",
        binding=SimpleNamespace(attempt_id="attempt", job_name="job"),
        package={"output_paths": []},
    )
    handoff = SimpleNamespace(
        operation_id="operation",
        admitted_attempt_id="attempt",
        job_uid="job-1",
        pod_uids=("pod-1",),
        target_digest="target",
        namespace="work",
        namespace_uid="namespace-uid",
        pvc_uid="pvc-uid",
        pvc_name="outputs",
        pvc_spec_sha256="spec",
        mount_path="/outputs",
        output_paths=(),
    )
    monkeypatch.setattr(module, "cluster_identity_digest", lambda _: "target")
    monkeypatch.setattr(module, "parse_job", lambda *_: SimpleNamespace(state="succeeded"))
    monkeypatch.setattr(module, "parse_pod", lambda *_: SimpleNamespace(state="succeeded"))
    collector = object.__new__(module.KubernetesOutputCollector)
    collector.client, collector.output = connection, output
    executions = []
    collector.execute = lambda *_: executions.append("collect") or {"outputs": []}
    if replacement:
        with pytest.raises(ValueError, match="stopped owned Pod set"):
            collector.collect(handoff, package)
        assert executions == []
    else:
        assert collector.collect(handoff, package) == ()
        assert executions == ["collect"]
    assert len(connection.calls) == 2
