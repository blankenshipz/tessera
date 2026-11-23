"""deploy-check names a missing guardian read config or old guardian (ADR 0041 stage 2)."""

from __future__ import annotations

import json
import os

from tessera_control import deploy_check
from tessera_control import deploy_state as ds
from tessera_control.guardian_record_reader import CONFIG_SCHEMA

CHECK = "providers.frame_verdict_read"
PROFILES = {"schema": "x", "profiles": [{}]}


def read_config(tmp_path):
    path = tmp_path / "guardian-read.json"
    path.write_text(
        json.dumps(
            {
                "schema": CONFIG_SCHEMA,
                "host": "guardian.internal",
                "port": 443,
                "token_file": "/etc/tessera/token",
                "ca_file": "/etc/tessera/ca.pem",
                "certificate_sha256": "a" * 64,
            }
        )
    )
    os.chmod(path, 0o600)
    return str(path)


def status_file(tmp_path, document):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(document))
    return path


GOOD_STATUS = {
    "frame_record": {
        "reservation": "r",
        "verdict": "open",
        "sequence": 0,
        "record_digest": "b" * 64,
    }
}


def test_disabled_has_no_failure():
    finding = ds.check_frame_verdict_read({"schema": "x"})
    assert finding.status == "skipped"


def test_enabled_without_config_fails_with_named_reason():
    finding = ds.check_frame_verdict_read({"salad_profiles": PROFILES})
    assert (finding.check_id, finding.status) == (CHECK, "fail")
    assert finding.detail["reason"] == "config_missing"
    assert finding.fix is not None
    report = deploy_check.build_report([finding])
    assert deploy_check.exit_code(report) == 1


def test_enabled_with_unusable_config_fails(tmp_path):
    document = {"salad_profiles": PROFILES, "salad_guardian_read": str(tmp_path / "nope")}
    assert ds.check_frame_verdict_read(document).detail["reason"] == "config_invalid"


def test_enabled_with_old_guardian_fails(tmp_path):
    document = {"salad_profiles": PROFILES, "salad_guardian_read": read_config(tmp_path)}
    old = status_file(tmp_path, {"state": "ok"})
    finding = ds.check_frame_verdict_read(document, old)
    assert finding.status == "fail"
    assert finding.detail["reason"] == "guardian_version_missing"


def test_enabled_complete_is_ok(tmp_path):
    document = {"salad_profiles": PROFILES, "salad_guardian_read": read_config(tmp_path)}
    finding = ds.check_frame_verdict_read(document, status_file(tmp_path, GOOD_STATUS))
    assert (finding.status, finding.detail["guardian_version_checked"]) == ("ok", True)
    unchecked = ds.check_frame_verdict_read(document)
    assert unchecked.status == "ok" and not unchecked.detail["guardian_version_checked"]


def test_unreadable_status_file_fails(tmp_path):
    document = {"salad_profiles": PROFILES, "salad_guardian_read": read_config(tmp_path)}
    finding = ds.check_frame_verdict_read(document, tmp_path / "missing.json")
    assert finding.detail["reason"] == "guardian_status_unreadable"
