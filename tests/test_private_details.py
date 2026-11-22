"""The tracked tree must not carry private or machine-specific details."""

import importlib.util
import re
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_private_details", Path(__file__).resolve().parents[1] / "dev/check_private_details.py"
)
lint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lint)


def test_tracked_files_have_no_private_details():
    assert lint.scan() == []


def test_patterns_flag_private_details_and_allow_placeholders():
    flag = lint.scan_text
    assert flag("docs/a.md", "cd /Users/alice/code") and flag("docs/a.md", "ssh /home/bob/x")
    assert not flag("docs/a.md", "/Users/user/.kube /home/example/x")
    assert flag("src/a.py", 'HOST = "192.168.1.20"') and flag("src/a.py", "ip 10.2.3.4")
    assert not flag("src/a.py", '"192.168.0.0/16"')
    assert not flag("tests/test_a.py", 'ip = "10.0.0.1"')
    assert flag("docs/a.md", "Track D lands later") and flag("docs/a.md", "not on this branch")
    assert flag("docs/a.md", "acme-corp", [re.compile("acme-corp")])
    assert not flag("docs/a.md", "nothing here")


def test_email_pattern_neither_over_nor_under_matches():
    flag = lambda text: lint.scan_text("src/a.py", text)  # noqa: E731
    # Real-looking addresses are flagged, including lookalikes of allowed domains.
    for text in (
        "mail alice@corp.io",
        "alice@mail.example-corp.com",
        "alice@example.evil.io",
        "alice@anthropic.com.evil.io",
        "alice@anthropic.computer",
        "alice@notest.com",
        "alice@foo.test-bar.com",
        "alice@host.timer.io",
        "alice@x.co.uk.",
    ):
        assert flag(text), text
    # Reserved/documentation domains, no-reply addresses and unit names are not.
    for text in (
        "alice@example.com",
        "alice@mail.example.org.",
        "alice@host.test",
        "alice@a.b.test, bob",
        "1+bob@users.noreply.github.com",
        "noreply@anthropic.com",
        "tessera-health-check@.service",
        "unit@host.service",
        "unit@host.example-x.timer",
    ):
        assert not flag(text), text
