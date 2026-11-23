"""The guardian record reader: closed frame_record shape, never raises, closed private config."""

from __future__ import annotations

import json
import os

import pytest

from tessera_control import guardian_record_reader as reader

RESERVATION = "a" * 64
DIGEST = "b" * 64
GOOD = {"reservation": RESERVATION, "verdict": "stopped", "sequence": 3, "record_digest": DIGEST}


class Client:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.sent = reply, error, []

    def _call(self, body):
        self.sent.append(body)
        if self.error:
            raise self.error
        return self.reply


def test_a_valid_record_is_returned_and_the_request_is_a_status_read():
    client = Client({"status": "stopped", "frame_record": dict(GOOD)})
    assert reader.HttpsGuardianRecordReader(client).read(RESERVATION) == GOOD
    assert client.sent == [{"operation": "status", "reservation": RESERVATION}]


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {**GOOD, "extra": 1},
        {k: v for k, v in GOOD.items() if k != "sequence"},
        {**GOOD, "verdict": "ok"},
        {**GOOD, "sequence": -1},
        {**GOOD, "sequence": True},
        {**GOOD, "sequence": "3"},
        {**GOOD, "record_digest": "B" * 64},
        {**GOOD, "record_digest": "b" * 63},
        {**GOOD, "reservation": "c" * 64},
    ],
)
def test_anything_but_the_closed_shape_for_this_reservation_is_none(record):
    reply = {"status": "stopped", "frame_record": record}
    assert reader.HttpsGuardianRecordReader(Client(reply)).read(RESERVATION) is None


@pytest.mark.parametrize("verdict", ["open", "stopped", "reconciled", "absent"])
def test_every_documented_verdict_is_accepted(verdict):
    client = Client({"frame_record": {**GOOD, "verdict": verdict}})
    assert reader.HttpsGuardianRecordReader(client).read(RESERVATION)["verdict"] == verdict


def test_a_transport_failure_or_a_non_object_reply_is_none_never_a_raise():
    assert reader.HttpsGuardianRecordReader(Client(error=OSError("down"))).read(RESERVATION) is None
    assert reader.HttpsGuardianRecordReader(Client(error=ValueError("x"))).read(RESERVATION) is None
    assert reader.HttpsGuardianRecordReader(Client(reply="nope")).read(RESERVATION) is None
    assert reader.HttpsGuardianRecordReader(Client(reply={})).read(RESERVATION) is None


def _config(tmp_path, mode=0o600, **over):
    document = {
        "schema": reader.CONFIG_SCHEMA,
        "host": "guardian.internal",
        "port": 443,
        "token_file": "/etc/guardian/token",
        "ca_file": "/etc/guardian/ca.pem",
        "certificate_sha256": "c" * 64,
        **over,
    }
    path = tmp_path / "guardian-read.json"
    path.write_text(json.dumps(document))
    os.chmod(path, mode)
    return path


def test_the_config_is_closed_private_and_builds_a_reader(tmp_path, monkeypatch):
    import tessera_control.salad_server as server

    built = {}

    class FakeClient:
        def __init__(self, **fields):
            built.update(fields)

    monkeypatch.setattr(server, "SaladSafetyHTTPSClient", FakeClient)
    assert reader.load_reader_config(_config(tmp_path))["host"] == "guardian.internal"
    assert isinstance(
        reader.build_https_reader(_config(tmp_path)), reader.HttpsGuardianRecordReader
    )
    assert built == {
        "host": "guardian.internal",
        "port": 443,
        "token_file": "/etc/guardian/token",
        "ca_file": "/etc/guardian/ca.pem",
        "certificate_sha256": "c" * 64,
    }


@pytest.mark.parametrize(
    "over",
    [
        {"schema": "other"},
        {"port": 8443},
        {"host": "bad/host"},
        {"host": ""},
        {"certificate_sha256": "zz"},
        {"token_file": "relative/token"},
        {"ca_file": 5},
        {"extra": "x"},
    ],
)
def test_a_config_with_an_unknown_key_or_bad_value_is_refused_without_echo(tmp_path, over):
    path = _config(tmp_path, **over)
    with pytest.raises(ValueError) as caught:
        reader.load_reader_config(path)
    text = str(caught.value)
    assert "relative/token" not in text and "bad/host" not in text and "zz" not in text


def test_a_missing_key_group_readable_or_non_json_config_is_refused(tmp_path):
    path = _config(tmp_path)
    document = json.loads(path.read_text())
    del document["ca_file"]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        reader.load_reader_config(path)
    with pytest.raises(ValueError):
        reader.load_reader_config(_config(tmp_path, mode=0o640))
    path.write_text("not json")
    with pytest.raises(ValueError):
        reader.load_reader_config(path)
    with pytest.raises(ValueError):
        reader.load_reader_config(tmp_path / "missing.json")
