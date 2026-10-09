"""Collector-derived public-source accounting, not a full-transcript claim."""


def attach_public_harness_accounting(conn, project_id, records):
    summaries = {}
    for record in records:
        harness = record.get("harness")
        if harness is None:
            continue
        key = (record["org_id"], record["credential_id"], harness["run_id"])
        if key not in summaries:
            row = conn.execute(
                "SELECT MAX(json_extract(envelope_json,'$.harness.source_watermark')) "
                "AS watermark, "
                "COUNT(DISTINCT json_extract(envelope_json,'$.harness.source_sequence')) "
                "AS recorded, "
                "MIN(json_extract(envelope_json,'$.harness.source_sequence')) AS first_sequence, "
                "MAX(sequence) AS collector_watermark, "
                "SUM(CASE WHEN json_extract(envelope_json,'$.harness.capture_state')='unknown' "
                "THEN 1 ELSE 0 END) AS unknown_events, "
                "SUM(COALESCE(json_array_length(json_extract(envelope_json,'$.harness.gaps')),0)) "
                "AS gap_reports FROM agent_provenance WHERE org_id=? AND project_id=? "
                "AND credential_id=? AND json_extract(envelope_json,'$.harness.run_id')=?",
                (record["org_id"], project_id, record["credential_id"], harness["run_id"]),
            ).fetchone()
            if row["unknown_events"]:
                completeness = "unknown_source_capture"
            elif row["gap_reports"]:
                completeness = "reported_source_gaps"
            elif row["recorded"] == row["watermark"] and row["first_sequence"] == 1:
                completeness = "complete_recorded_public_metadata_through_reported_watermark"
            else:
                completeness = "missing_source_events"
            summaries[key] = {
                "source_completeness": completeness,
                "reported_source_watermark": row["watermark"],
                "recorded_source_events": row["recorded"],
                "missing_source_event_count": max(0, row["watermark"] - row["recorded"]),
                "reported_gap_range_count": row["gap_reports"],
                "unknown_capture_event_count": row["unknown_events"],
                "collector_sequence_watermark": row["collector_watermark"],
                "capture_authorization": "immutable_human_context_policy",
                "source_attribution": "self_reported_harness_metadata",
                "boundary": "approved_public_metadata_only",
                "raw_transcript": "not_captured",
                "hidden_reasoning": "not_captured",
                "unseen_sources_and_beyond_watermark": "unknown",
                "authority_effect": "none",
            }
        record["harness_capture"] = summaries[key]
    return records
