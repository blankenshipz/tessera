"""ADR numbers are unique: "ADR NNNN" in a receipt, README or comment names exactly one file."""

import re
from collections import defaultdict
from pathlib import Path

DECISIONS = Path(__file__).parents[1] / "docs" / "decisions"

# Two older ADRs already share 0027. That is frozen: no other number may be shared, and this
# entry may not be copied for a new duplicate (reserve numbers with the allocator instead).
LEGACY_SHARED = {
    "0027": {
        "0027-external-agent-adapter.md",
        "0027-schema-maintenance-observation-contract.md",
    }
}


def _by_number():
    found = defaultdict(set)
    for path in DECISIONS.glob("*.md"):
        match = re.match(r"(\d{4})-", path.name)
        if match:
            found[match.group(1)].add(path.name)
    return found


def test_no_two_adrs_share_a_number():
    shared = {n: names for n, names in _by_number().items() if len(names) > 1}
    unexpected = {n: sorted(names) for n, names in shared.items() if names != LEGACY_SHARED.get(n)}
    assert unexpected == {}, f"duplicate ADR numbers (reserve a new one): {unexpected}"


def test_the_legacy_exception_is_still_needed():
    # Delete the allowlist entry the day the duplicate is renumbered.
    shared = {n for n, names in _by_number().items() if len(names) > 1}
    assert set(LEGACY_SHARED) <= shared
    # The allowlist is frozen at the one legacy duplicate; it may only shrink, never grow.
    assert set(LEGACY_SHARED) == {"0027"}


def test_a_title_that_names_a_number_names_its_own():
    for path in DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"):
        title = path.read_text().splitlines()[0]
        # Titles read "# NNNN: ...", "# NNNN — ..." or "# ADR NNNN: ..."; any other 4-digit
        # number in a title (a year, a count) is not an ADR number.
        named = re.match(r"# (?:ADR )?(\d{4})\b", title)
        assert named is None or named.group(1) == path.name[:4], f"{path.name}: {title!r}"
