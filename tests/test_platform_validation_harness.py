"""Offline tests for the pure logic of the opt-in platform validation harness.

The harness lives in ``scripts/platform_validation`` and is not collected by default
pytest; these tests cover only its decision logic (no Docker, no network, no service).
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).parents[1] / "scripts")


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    from platform_validation import check_real_run, common, real_run

    return SimpleHarness(check_real_run, common, real_run)


class SimpleHarness:
    def __init__(self, check_real_run, common, real_run):
        self.check_real_run = check_real_run
        self.common = common
        self.real_run = real_run


# -- direct-connect classification (1b) ---------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "verdict"),
    [
        # Only "no route" or "nothing answered" proves the bypass is closed.
        ({"connected": False, "errno_name": "ENETUNREACH"}, "blocked"),
        ({"connected": False, "errno_name": "EHOSTUNREACH"}, "blocked"),
        ({"connected": False, "errno_name": "TIMEOUT"}, "blocked"),
        # A completed connection, a refusal or a reset means packets reached a host.
        ({"connected": True}, "reached"),
        ({"connected": False, "errno_name": "ECONNREFUSED"}, "reached"),
        ({"connected": False, "errno_name": "ECONNRESET"}, "reached"),
        # Anything else cannot decide the question.
        ({"connected": False, "errno_name": "EACCES"}, "undecided"),
        ({"connected": False, "errno_name": "EPERM"}, "undecided"),
        ({"connected": False, "errno_name": None}, "undecided"),
        ({"connected": False}, "undecided"),
        ({"connected": None, "error_class": "RuntimeError"}, "undecided"),
        ({}, "undecided"),
    ],
)
def test_direct_connect_classification(harness, observation, verdict):
    assert harness.check_real_run.classify_direct_connect(observation) == verdict


def test_the_blocked_and_reached_sets_are_disjoint(harness):
    module = harness.check_real_run
    assert not (module._BLOCKED & module._REACHED)
    assert module._BLOCKED == {"ENETUNREACH", "EHOSTUNREACH", "TIMEOUT"}


def test_the_workload_reports_symbolic_errno_names_and_sets_no_global_timeout(harness):
    # Structural, not textual: parse the workload and inspect its calls and attributes.
    tree = ast.parse(harness.check_real_run.EGRESS_WORKLOAD)
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    attributes = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    # The errno name comes from the container's own (Linux) errno table, not the host's.
    assert "errno.errorcode.get" in calls
    # No process-global socket timeout: it would change every later (proxied) socket.
    assert "socket.setdefaulttimeout" not in calls | attributes
    # The direct DNS lookup is bounded by a per-call alarm that is cleared afterwards.
    assert {"signal.alarm", "signal.signal"} <= calls


# -- driver residue reporting (M4) ---------------------------------------------------------


def make_driver(harness):
    driver = harness.real_run.RealRun.__new__(harness.real_run.RealRun)
    driver.operations = ["op1"]
    driver.adopted = {"containers": set(), "networks": set(), "volumes": set()}
    driver.unattributed = []
    driver.cleanup_errors = []
    return driver


def test_a_failed_post_run_listing_is_recorded_so_residue_is_a_gap(harness, monkeypatch):
    driver = make_driver(harness)
    before = {"containers": {"x"}, "networks": set(), "volumes": set()}

    def broken():
        raise RuntimeError("docker ps listing failed")

    monkeypatch.setattr(harness.real_run, "_tessera_resources", broken)
    assert driver._snapshot_after(before) is before
    assert driver.unattributed and "post-run resource listing failed" in driver.unattributed[0]
    monkeypatch.setattr(driver, "residue", lambda: [])
    status, evidence = driver.residue_report([])
    assert status == "GAP" and "could not be established" in evidence


def test_residue_report_levels(harness, monkeypatch):
    driver = make_driver(harness)
    monkeypatch.setattr(driver, "residue", lambda: [])
    assert driver.residue_report(["containers:tessera-op-op1"])[0] == "PASS"
    assert driver.residue_report(["networks:tessera-egress-op1"])[0] == "FAIL"
    monkeypatch.setattr(driver, "residue", lambda: ["containers:tessera-op-op1"])
    assert driver.residue_report([])[0] == "FAIL"
    monkeypatch.setattr(driver, "residue", lambda: [])
    driver.cleanup_errors.append("containers:tessera-op-op1: docker rm timed out")
    assert driver.residue_report([])[0] == "FAIL"


def test_exact_names_are_derived_from_the_operation_id(harness):
    names = harness.real_run._exact_names
    assert names("containers", "abc") == {"tessera-op-abc", "tessera-egress-proxy-abc"}
    assert names("networks", "abc") == {"tessera-egress-abc"}
    assert names("volumes", "abc") == frozenset()


def test_ownership_is_an_exact_name_or_label_never_a_substring(harness, monkeypatch):
    belongs = harness.real_run.resource_belongs_to
    calls = []

    class Done:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    def docker(*args, **kwargs):
        calls.append(args)
        return Done(0, '{"tessera.operation": "someone-else"}')

    monkeypatch.setattr(harness.real_run, "_docker", docker)
    # Exact name: owned without asking Docker.
    assert belongs("containers", "tessera-op-abc", "abc") is True and not calls
    # A name that merely contains the id is not owned; the label decides.
    assert belongs("containers", "tessera-op-abc-other", "abc") is False
    monkeypatch.setattr(
        harness.real_run, "_docker", lambda *a, **k: Done(0, '{"tessera.operation": "abc"}')
    )
    assert belongs("volumes", "some-volume", "abc") is True
    # An unreadable label is unknown, not adopted and not silently ignored.
    monkeypatch.setattr(harness.real_run, "_docker", lambda *a, **k: Done(1))
    assert belongs("volumes", "some-volume", "abc") is None
    assert belongs("volumes", "some-volume", "") is False


# -- database helpers close their connections (L12) ------------------------------------------


def test_db_helpers_close_every_connection(harness, tmp_path, monkeypatch):
    database = tmp_path / "tessera.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (n INTEGER)")
    connection.execute("INSERT INTO t VALUES (7)")
    connection.commit()
    connection.close()
    opened = []
    real_connect = sqlite3.connect

    class Tracked:
        def __init__(self, inner):
            self.inner, self.closed = inner, False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.closed = True
            self.inner.close()

    def tracking(*args, **kwargs):
        tracked = Tracked(real_connect(*args, **kwargs))
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(sqlite3, "connect", tracking)
    assert harness.common.db_rows(tmp_path, "SELECT n FROM t") == [(7,)]
    assert harness.common.db_counts(tmp_path, {"n": "SELECT count(*) FROM t"}) == {"n": 1}
    with pytest.raises(sqlite3.OperationalError):
        harness.common.db_rows(tmp_path, "SELECT * FROM missing_table")
    assert len(opened) == 3 and all(c.closed for c in opened)
