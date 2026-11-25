"""The optional ``allowed_license_labels`` key of the provider trust file."""

from __future__ import annotations

import pytest

from tessera_control.provider_trust import (
    TrustPolicyRefused,
    policy_from_document,
)


def document(entry_extra=None, mode=None, **providers):
    entry = {"data_classes": ["public"], "worker_principal_ids": ["w1"]}
    entry.update(entry_extra or {})
    doc = {"schema": "tessera.provider-trust.v1", "providers": {"remote": entry, **providers}}
    if mode:
        doc["mode"] = mode
    return doc


def test_an_absent_key_leaves_the_canonical_form_and_digest_as_before():
    plain = policy_from_document(document(), "file")
    assert "allowed_license_labels" not in plain.document["providers"]["remote"]
    assert plain.document["providers"]["remote"] == {
        "data_classes": ["public"],
        "worker_principal_ids": ["w1"],
    }
    # 'remote' is not registered in this process, so not known to be operator-owned: it
    # receives unlabelled data only (ADR 0113 amendment 2026-11-23); the digest is unchanged.
    assert plain.allowed_license_labels("remote") == {"unlabelled"}
    # a provider the file does not list is refused anything labelled (fail closed)
    assert plain.allowed_license_labels("unlisted") == {"unlabelled"}
    assert not plain.lists_license_labels("unlisted")
    assert not plain.lists_license_labels("remote")
    assert plain.allowed_license_labels(None) is None
    again = policy_from_document(document(), "file")
    assert again.digest == plain.digest


def test_a_set_key_is_canonical_sorted_and_changes_the_digest():
    plain = policy_from_document(document(), "file")
    rule = policy_from_document(
        document({"allowed_license_labels": ["cc-by", "unlabelled", "a-label"]}), "file"
    )
    assert rule.document["providers"]["remote"]["allowed_license_labels"] == [
        "a-label",
        "cc-by",
        "unlabelled",
    ]
    assert rule.allowed_license_labels("remote") == {"a-label", "cc-by", "unlabelled"}
    assert rule.digest != plain.digest
    same = policy_from_document(
        document({"allowed_license_labels": ["unlabelled", "a-label", "cc-by"]}), "file"
    )
    assert same.digest == rule.digest  # order does not matter


@pytest.mark.parametrize(
    "value",
    [
        [],
        "cc-by",
        ["cc-by", "cc-by"],
        ["Cc-By"],
        ["1abc"],
        ["x" * 33],
        [3],
        ["unknown-lineage"],
        [f"l{i}" for i in range(17)],
    ],
)
def test_a_malformed_allowed_license_labels_refuses_the_file(value):
    with pytest.raises(TrustPolicyRefused, match="allowed_license_labels"):
        policy_from_document(document({"allowed_license_labels": value}), "file")


def test_other_unknown_provider_keys_are_still_refused():
    with pytest.raises(TrustPolicyRefused):
        policy_from_document(document({"license_labels": ["x"]}), "file")
    with pytest.raises(TrustPolicyRefused):
        policy_from_document(
            {"schema": "tessera.provider-trust.v1", "providers": {"remote": {"data_classes": []}}},
            "file",
        )


def test_the_restriction_holds_in_every_mode():
    rule = {"allowed_license_labels": ["cc-by"]}
    for mode in (None, "all_registered_providers"):
        policy = policy_from_document(document(rule, mode=mode), "file")
        assert policy.allowed_license_labels("remote") == {"cc-by"}


def test_a_listed_provider_without_the_key_is_unrestricted_only_when_operator_owned():
    from tessera_control.provider_trust import builtin_policy

    own = {"data_classes": ["public"], "worker_principal_ids": ["w2"]}
    assert document()["providers"]["remote"].get("allowed_license_labels") is None
    listed = policy_from_document(document(**{"local-docker": own}), "file")
    # The built-in local provider is the operator's own capacity: unrestricted as before.
    assert listed.allowed_license_labels("local-docker") is None
    # ADR 0113 amendment 2026-11-23: an unregistered (or third-party) id receives unlabelled
    # data only until its entry names a label.
    assert listed.allowed_license_labels("remote") == {"unlabelled"}
    assert not listed.lists_license_labels("remote")
    assert builtin_policy().allowed_license_labels("anything") == {"unlabelled"}
