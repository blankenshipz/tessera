"""Container-log tail: transport query, quota labelling, and the fail-closed sanitizer.

Fakes only: no provider, no network, no credentials."""

import hashlib
import json

import pytest
from test_salad_http import Factory, run

from tessera_control.salad_container_log import (
    closed_container_log,
    sanitize_line,
    summarize_container_log,
    unavailable,
)


def entry(text, key="text_log"):
    return {key: text if key == "text_log" else {"message": text}}


def body(*texts):
    return {"organization_name": "acme", "items": [entry(t) for t in texts]}


def test_transport_builds_exact_query_and_returns_closed_tail():
    f = Factory((200, body("newest line", "older line")))
    result = run(f, "read_logs")
    assert result.status == "observed" and len(f.calls) == 1
    method, path, raw, _ = f.calls[0]
    assert method == "POST" and path == "/api/public/organizations/acme/log-entries"
    sent = json.loads(raw)
    assert set(sent) == {"start_time", "end_time", "page_size", "sort_order", "query"}
    assert sent["page_size"] == 50 and sent["sort_order"] == "desc"
    assert sent["query"].startswith('resource.type = "container" AND ')
    assert 'project_name = "research"' in sent["query"]
    assert 'container_group_name = "attempt-' in sent["query"]
    tail = result.observation["container_log"]
    assert closed_container_log(tail) == tail
    assert tail["lines"] == ["older line", "newest line"]


def test_429_is_log_quota_labelled_read_logs():
    f = Factory((429, {"message": "daily limit"}))
    result = run(f, "read_logs")
    assert len(f.calls) == 1 and result.status == "unknown"
    assert result.detail == "rate_limited" and result.request_kind == "read_logs"


def test_read_events_kind_unchanged():
    result = run(Factory((429, {})), "read_events")
    assert result.request_kind == "read_events"


@pytest.mark.parametrize("data", [{"items": "x"}, {"nope": 1}, {"items": None}])
def test_malformed_page_fails_closed(data):
    result = run(Factory((200, data)), "read_logs")
    assert result.status == "unknown" and result.observation is None


@pytest.mark.parametrize(
    "items", [None, "x", 5, [None, 3, "s", {"text_log": 7}, {"json_log": [1]}], []]
)
def test_malformed_items_are_empty(items):
    out = summarize_container_log(items)
    assert out["outcome"] == "empty" and out["lines"] == [] and out["untrusted"] is True
    assert closed_container_log(out) == out


def test_chronological_order_and_json_log():
    items = [entry("third"), entry("second", "json_log"), entry("first")]
    assert summarize_container_log(items)["lines"] == ["first", "second", "third"]


def test_line_bound_keeps_last_twenty():
    items = [entry(f"line {i}") for i in range(30, 0, -1)]  # desc
    out = summarize_container_log(items)
    assert out["lines"] == [f"line {i}" for i in range(11, 31)] and out["truncated"] is True


def test_byte_bound_drops_from_front():
    items = [entry(f"{i:02d}" + " w" * 74) for i in range(20, 0, -1)]
    out = summarize_container_log(items)
    assert sum(len(x) + 1 for x in out["lines"]) - 1 <= 1024
    assert out["truncated"] is True and out["lines"][-1].startswith("20")


def test_line_char_cap():
    out = summarize_container_log([entry("a b " * 100)])
    assert all(len(x) <= 200 for x in out["lines"])


def test_sha256_matches_joined_lines():
    out = summarize_container_log([entry("b"), entry("a")])
    assert out["sha256"] == hashlib.sha256(b"a\nb").hexdigest()


def test_field_case_line_survives():
    line = "expected exactly one input named shampoo_features.json.gz, found 0"
    assert sanitize_line(line) == line
    assert summarize_container_log([entry(line)])["lines"] == [line]


def test_non_printables_replaced():
    assert sanitize_line("a\x00b\x1b[31mé\tc") == "a?b?[31m??c"


# Home-directory prefixes, built at runtime so no personal-looking path is a literal here.
HOME = "/" + "home" + "/"
MAC_HOME = "/" + "Users" + "/"

SECRETS = [
    ("password=hunter2hunter", "hunter2hunter"),
    ("Authorization: Bearer abc123def", "abc123def"),
    ('api_key="zzTopSecret"', "zzTopSecret"),
    ("cookie: sid-value", "sid-value"),
    ("blob " + "A1b2C3d4" * 4, "A1b2C3d4A1b2"),
    ("hex " + "0123456789abcdef" * 2, "0123456789abcdef0123"),
    ("jwt aaaa.bbbbbbbb.cccccccc", "bbbbbbbb"),
    ("fetch https://bob:pw9@example.test/x", "pw9"),
    ("mail alice@example.test now", "alice@example.test"),
    ("peer 203.0.113.77 down", "203.0.113.77"),
    ("peer 2001:db8::1 down", "2001:db8::1"),
    ("id 550e8400-e29b-41d4-a716-446655440000", "550e8400"),
    ("at " + HOME + "carol/work/x.py", "carol"),
    ("at " + MAC_HOME + "dave/x", "dave"),
    ("at /root/x", "/root"),
    # q-2716 review F1: the scheme word is kept, the credential after it is redacted.
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Authorization: Bearer abc123", "abc123"),
    ("Authorization: Token tok9", "tok9"),
    ("sent Basic qwer1234 upstream", "qwer1234"),
    ("retry with Bearer zz9", "zz9"),
    # "<keyword> is <value>" phrasing redacts the value, not the connecting word.
    ("the password is hunter2", "hunter2"),
    ("token was t0ken7", "t0ken7"),
    # "api key" / "access key" with a space.
    ("api key: k3y9", "k3y9"),
    ("ACCESS KEY abc7", "abc7"),
    # Well-known credential prefixes, whatever their length.
    ("use sk-ab1 now", "sk-ab1"),
    ("gh ghp_x1 ok", "ghp_x1"),
    ("gh gho_y2 ok", "gho_y2"),
    ("slack xoxb-1-2 ok", "xoxb-1-2"),
    ("slack xoxp-9 ok", "xoxp-9"),
    ("aws AKIAABCD12 ok", "AKIAABCD12"),
]


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic [redacted]"),
        ("the password is hunter2", "the password is [redacted]"),
        ("use sk-ab1 now", "use [redacted] now"),
    ],
)
def test_redaction_keeps_the_context_words(text, kept):
    assert sanitize_line(text) == kept


def test_ordinary_error_lines_are_unchanged():
    for line in ("Traceback (most recent call last):", "ValueError: bad shape (3, 4)"):
        assert sanitize_line(line) == line


@pytest.mark.parametrize(("text", "secret"), SECRETS)
def test_redaction(text, secret):
    out = summarize_container_log([entry(text)])
    assert secret not in json.dumps(out)
    assert out["lines"]


def test_home_paths_become_tilde():
    assert sanitize_line("at " + HOME + "carol/work/x.py") == "at ~/work/x.py"


def test_unavailable_closed():
    assert unavailable("log_quota_exhausted") == {
        "outcome": "unavailable",
        "reason": "log_quota_exhausted",
    }
    assert unavailable("bogus")["reason"] == "read_failed"
    assert closed_container_log(unavailable("not_read"))


def good():
    return summarize_container_log([entry("hello")])


def test_closed_rejects_bad_shapes():
    g = good()
    assert closed_container_log(g) == g
    assert closed_container_log(dict(g, extra=1)) is None
    assert closed_container_log(dict(g, lines=["x"] * 21)) is None
    assert closed_container_log(dict(g, lines=["x" * 201])) is None
    assert closed_container_log(dict(g, lines=["é"])) is None
    assert closed_container_log(dict(g, lines=[3])) is None
    assert closed_container_log(dict(g, truncated=1)) is None
    assert closed_container_log(dict(g, untrusted=False)) is None
    assert closed_container_log(dict(g, sha256="0" * 64)) is None
    assert closed_container_log(dict(g, outcome="weird")) is None
    assert closed_container_log({"outcome": "unavailable", "reason": "x"}) is None
    assert closed_container_log({"outcome": "unavailable", "reason": "not_read", "a": 1}) is None
    assert closed_container_log("x") is None
    big = ["y" * 150] * 8
    assert (
        closed_container_log(
            dict(
                g, lines=big, sha256=hashlib.sha256(b"\n".join(x.encode() for x in big)).hexdigest()
            )
        )
        is None
    )
