"""Only independently human-admitted registered final output; all other capture unknown."""

import hashlib

from .research_consumer_store import ConsumerBlocked
from .v2_public_output import V2FinalOutputReader


def read_registered_final_metadata(store, binding, harness, records, row):
    if binding["allowed_public_events"] != ["generated_output"]:
        raise ConsumerBlocked("final_output_only_capture_policy_required")
    state = store.get(row["id"])
    if state["output_id"] is None:
        return []  # Not a claim that public interaction coverage is complete.
    output, _ = records.output(state["output_id"])
    harness.verify()
    page = harness.endpoint.call(
        "GET", f"/api/session/{row['session_id']}/message?type=assistant&order=desc&limit=100"
    )
    messages = page.get("data")
    if not isinstance(messages, list) or len(messages) > 100:
        raise ConsumerBlocked("registered_final_source_page_unknown")
    matches = [
        m
        for m in messages
        if isinstance(m, dict)
        and isinstance(m.get("id"), str)
        and "final_" + hashlib.sha256((row["session_id"] + "\n" + m["id"]).encode()).hexdigest()
        == output["source_event_id"]
    ]
    if len(matches) != 1:
        raise ConsumerBlocked("registered_final_source_unknown")
    final = V2FinalOutputReader(harness)._project(row, matches[0], output["source_sequence"])
    if final.digest != output["sha256"]:
        raise ConsumerBlocked("registered_final_body_changed")
    event = {
        k: binding[k]
        for k in (
            "run_id",
            "session_id",
            "harness_instance_id",
            "task_id",
            "context_id",
            "context_digest",
            "skill_digest",
            "config_digest",
            "model_id",
            "input_context_id",
            "input_body_digest",
            "capture_policy_context_id",
        )
    }
    return [
        {
            **event,
            "schema_version": "tessera-normalized-public-observation-v1",
            "public_event": "generated_output",
            "source_event_id": output["source_event_id"],
            "source_sequence": output["source_sequence"],
            "source_watermark": output["source_sequence"],
            "capture_state": "unknown",
            "gaps": [],
            "outcome": "succeeded",
            "registered_output_id": output["id"],
        }
    ]
