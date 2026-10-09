from __future__ import annotations

from tessera_control.activation_gates import ROLE_ACTIONS, CollaborationAction, CollaborationRole


def test_future_organization_roles_have_separate_default_actions() -> None:
    assert ROLE_ACTIONS[CollaborationRole.RESEARCHER] == frozenset({CollaborationAction.DRAFT_PLAN})
    assert ROLE_ACTIONS[CollaborationRole.APPROVER] == frozenset(
        {CollaborationAction.APPROVE_EXECUTION}
    )
    assert ROLE_ACTIONS[CollaborationRole.REVIEWER] == frozenset(
        {CollaborationAction.REVIEW_EVIDENCE}
    )
    assert ROLE_ACTIONS[CollaborationRole.OPERATOR] == frozenset(
        {CollaborationAction.OPERATE_INFRASTRUCTURE}
    )


def test_no_prospective_role_combines_research_approval_review_and_operation() -> None:
    consequential_actions = frozenset(CollaborationAction)
    assert all(actions < consequential_actions for actions in ROLE_ACTIONS.values())
