"""Reject recognizable provider-key labels in Tessera authentication enrollment.

Opaque random bytes have no reliably detectable provider. Purpose is enforced by
separate enrollment APIs, credential registration and authenticated audience, not
by claiming a prefix heuristic recognizes all provider keys.
"""


def provider_key_shaped(value: str) -> bool:
    return value.lower().startswith(("salad", "sld_", "sld-", "sk-", "sk_", "ai_gateway"))
