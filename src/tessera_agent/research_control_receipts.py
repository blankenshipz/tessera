"""Project API consumption receipt identities, never cached attempt state as polling."""

import re

from .research_consumer_store import ConsumerBlocked


def consumption_reference(response, grant_id, action):
    if (
        not isinstance(response, dict)
        or response.get("authority_grant_id") != grant_id
        or response.get("action") != action
        or not isinstance(response.get("result"), dict)
    ):
        raise ConsumerBlocked("control_consumption_receipt_unknown")
    result = response["result"]
    reference = {
        "consumption_id": response.get("consumption_id"),
        "result_id": result.get("id"),
        "operation_id": result.get("operation_id"),
    }
    for key, value in reference.items():
        if key == "operation_id" and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value
        ):
            raise ConsumerBlocked("control_consumption_receipt_unknown")
    return reference
