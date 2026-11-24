"""The standing loop's action for a stuck-campaign finding: offer it to the operator, on receipt.

``StuckOfferHandler`` is the loop's ``start_call``. It calls no model and acts on no campaign.
For one finding it writes one durable ``stuck_offered`` receipt to the runner journal (ids, codes
and digests only: the allowlisted fields ``StuckWorkSource`` already validated, never free text
from the control plane). With ``ack_on_offer`` it first acknowledges the finding through the
control's acknowledge route (fixed note, idempotency key ``loop-ack:<item id>``), which marks the
finding seen and leaves the campaign untouched.

Outcomes, in the loop's terms:

* kill switch engaged, a model other than ``none``, an unknown code or a malformed item:
  ``CallNotStarted`` (nothing was done);
* the acknowledgement was definitely refused (HTTP 4xx): ``CallNotStarted`` (counted as a failure,
  backed off and halted at the loop's cap; nothing was changed and no receipt is written);
* the acknowledgement may have been accepted (5xx, no answer, transport error): a plain
  ``StuckAckUncertain``; the loop records ``uncertain_outcome`` and never retries it by itself;
* otherwise the receipt is written and the call succeeds (the ledger row becomes ``succeeded``).

The acknowledgement runs before the receipt so that ``CallNotStarted`` always means nothing at all
happened; the receipt records whether the finding was acknowledged.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from tessera_agent.standing_loop import CallNotStarted, KillSwitchEngaged
from tessera_agent.stuck_loop_control import FINDING_ID, StuckControlError
from tessera_agent.stuck_work_source import CODES, KIND

NO_MODEL = "none"
_ITEM_ID = re.compile(rf"(?P<fid>{FINDING_ID}):(?P<n>[1-9][0-9]*)\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ACTION = re.compile(r"[a-z0-9_]{1,100}\Z")
_KEY_MAX = 128


class StuckAckUncertain(Exception):
    """The acknowledgement may have been accepted; the loop must not retry it by itself."""


def ack_note(policy_digest: str) -> str:
    return f"offered to the operator by the agent loop {policy_digest[:12]}"


def ack_key(item_id: str) -> str:
    key = f"loop-ack:{item_id}"
    if len(key) <= _KEY_MAX:
        return key
    return "loop-ack:sha256:" + hashlib.sha256(item_id.encode()).hexdigest()


class StuckOfferHandler:
    """``control.call(method, path, body=None, key=None) -> dict``; ``record(event)`` is the
    durable journal writer; ``kill_switch.engaged()`` is re-checked before anything is done."""

    def __init__(
        self,
        control: Any,
        project_id: str,
        record: Callable[[dict], None],
        kill_switch: Any,
        ack_on_offer: bool = False,
    ) -> None:
        if not isinstance(project_id, str) or not _SEGMENT.fullmatch(project_id):
            raise ValueError("project_id is not a plain identifier")
        if not isinstance(ack_on_offer, bool):
            raise ValueError("ack_on_offer must be a bool")
        self.control, self.project_id, self.record = control, project_id, record
        self.kill_switch, self.ack_on_offer = kill_switch, ack_on_offer

    def __call__(
        self, item: dict, *, model: str, tier: str, identity_ref: str, policy_digest: str
    ) -> None:
        if self.kill_switch.engaged():
            raise KillSwitchEngaged("kill_switch")
        if model != NO_MODEL or tier != NO_MODEL:
            raise CallNotStarted("model_not_allowed")
        fields = self._fields(item)
        if fields is None:
            raise CallNotStarted("malformed_item")
        acknowledged = False
        if self.ack_on_offer:
            self._acknowledge(fields["finding_id"], fields["item_id"], policy_digest)
            acknowledged = True
        self.record(
            {
                "event": "stuck_offered",
                **fields,
                "policy_digest": policy_digest,
                "acknowledged": acknowledged,
            }
        )

    def _acknowledge(self, finding_id: str, item_id: str, policy_digest: str) -> None:
        path = f"/projects/{self.project_id}/campaign-stuck/{finding_id}/acknowledge"
        try:
            self.control.call(
                "POST", path, body={"note": ack_note(policy_digest)}, key=ack_key(item_id)
            )
        except StuckControlError as error:
            if error.definite_refusal:
                raise CallNotStarted(f"ack_refused_{error.status}") from None
            raise StuckAckUncertain("ack_outcome_unknown") from None
        except ValueError:  # refused by the adapter's allowlist: no request was made
            raise CallNotStarted("ack_not_allowed") from None
        except Exception:  # noqa: BLE001 - the request may have reached the control
            raise StuckAckUncertain("ack_outcome_unknown") from None

    @staticmethod
    def _fields(item: Any) -> dict[str, Any] | None:
        """The allowlisted fields of a well-formed item, or None."""
        if not isinstance(item, dict) or item.get("kind") != KIND:
            return None
        item_id, code = item.get("id"), item.get("code")
        campaign, stage = item.get("campaign_id"), item.get("stage_key")
        action, digest = item.get("suggested_action"), item.get("receipt_sha256")
        match = _ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if (
            match is None
            or code not in CODES
            or not isinstance(campaign, str)
            or not _SEGMENT.fullmatch(campaign)
            or not (stage is None or (isinstance(stage, str) and _SEGMENT.fullmatch(stage)))
            or not isinstance(action, str)
            or not _ACTION.fullmatch(action)
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            return None
        return {
            "item_id": item_id,
            "finding_id": match["fid"],
            "occurrence": int(match["n"]),
            "code": code,
            "suggested_action": action,
            "campaign_id": campaign,
            "stage_key": stage,
            "receipt_sha256": digest,
        }
