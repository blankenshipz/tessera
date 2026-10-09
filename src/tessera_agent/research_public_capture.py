"""Supervisor callsite for an approved normalized public metadata producer.

This is not a raw V2 parser. The producer must provide eligible public source
identity/sequence/watermarks after actual installed schema/disclosure admission.
Unsupported or unavailable producers block, never synthesize events from usage.
"""

from .remote_reasoning_runtime import RuntimeBlocked


class PublicStatusCapture:
    def __init__(self, reader, outbox, collector):
        self.reader, self.outbox, self.collector = reader, outbox, collector

    def after_status(self, row, observation):
        try:
            events = self.reader.read_public_metadata(row)
            if not isinstance(events, list) or len(events) > 8:
                raise ValueError()
            for event in events:
                if event.get("run_id") != row["id"] or event.get("session_id") != row["session_id"]:
                    raise ValueError()
                # Existing adapter/outbox rechecks immutable human policy and pins.
                self.outbox.capture(event)
            self.outbox.deliver_next(self.collector)
        except Exception:
            raise RuntimeBlocked("public_capture_source_or_delivery_unknown") from None
