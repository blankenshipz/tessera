"""Validated approved normalized source port, never a V2/private stream extractor."""

from .public_capture import PublicObservation


class NormalizedPublicMetadataProducer:
    """Runtime may inject its actual approved normalized metadata reader here.

    No source sequence is assigned by counting stream parts. Unsupported raw/private
    records fail closed, are not logged/persisted, and create no invented public gaps.
    Extraction and independently observed V2 public visibility remain runtime-owned.
    """

    def __init__(self, adapter, read_approved_metadata, *, batch_limit=100):
        if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
            raise ValueError("Public metadata batch must be bounded.")
        self.adapter = adapter
        self.reader = read_approved_metadata
        self.batch_limit = batch_limit

    def read_public_metadata(self, row):
        try:
            if (
                row["id"] != self.adapter.binding["run_id"]
                or row["session_id"] != self.adapter.binding["session_id"]
            ):
                raise ValueError()
            self.adapter.resolve_contexts()
            result = []
            for observation in self.reader(row):
                if len(result) >= self.batch_limit:
                    raise ValueError()
                typed = PublicObservation.model_validate(observation).model_dump(mode="json")
                self.adapter.envelope(typed)
                result.append(typed)
            return result
        except Exception:
            raise ValueError(
                "Approved normalized public metadata is unavailable or unsupported."
            ) from None
