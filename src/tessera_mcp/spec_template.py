"""Render a campaign spec from a template plus values (ADR 0124).

A template is a campaign spec as JSON in which any string may hold ``${NAME}`` placeholders
(``NAME`` is upper-case letters, digits and underscores). Rendering is structural: the template
is parsed first and values are substituted only inside string leaves, so a value can never add a
key, close a string or change a type. Every placeholder needs a value and every value must be
used, so a typo is refused instead of silently submitting a different spec. The result is an
ordinary spec: the control plane validates it, and a person approves its digest as always.
"""

from __future__ import annotations

import json
import re
from typing import Any

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
MAX_TEMPLATE_BYTES = 512 * 1024
MAX_VALUE_CHARS = 4096
MAX_VALUES = 256


class TemplateError(ValueError):
    """The template or values cannot produce a spec; nothing was sent anywhere."""


def _walk(node: Any, used: set[str], missing: set[str], values: dict[str, str]) -> Any:
    if isinstance(node, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in values:
                missing.add(name)
                return match.group(0)
            used.add(name)
            return values[name]

        return PLACEHOLDER.sub(replace, node)
    if isinstance(node, list):
        return [_walk(item, used, missing, values) for item in node]
    if isinstance(node, dict):
        # Keys are structure, not data: a placeholder in a key is refused.
        for key in node:
            if PLACEHOLDER.search(key):
                raise TemplateError("A placeholder is not allowed in an object key.")
        return {key: _walk(item, used, missing, values) for key, item in node.items()}
    return node


def render_spec(template: str | dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Return the spec dict, or raise TemplateError naming the missing or unused values."""
    if isinstance(template, str):
        if len(template.encode()) > MAX_TEMPLATE_BYTES:
            raise TemplateError("The template is too large.")
        try:
            template = json.loads(template)
        except ValueError as error:
            raise TemplateError("The template is not valid JSON.") from error
    if not isinstance(template, dict):
        raise TemplateError("The template must be a JSON object (a campaign spec).")
    if len(values) > MAX_VALUES:
        raise TemplateError("Too many values.")
    for name, value in values.items():
        if not PLACEHOLDER.fullmatch("${" + name + "}"):
            raise TemplateError(f"Value name {name!r} is not UPPER_CASE letters, digits, _.")
        if not isinstance(value, str) or len(value) > MAX_VALUE_CHARS:
            raise TemplateError(f"Value {name} must be a string of at most {MAX_VALUE_CHARS}.")
    used: set[str] = set()
    missing: set[str] = set()
    spec = _walk(template, used, missing, values)
    if missing:
        raise TemplateError(f"Missing values for: {sorted(missing)}")
    unused = sorted(set(values) - used)
    if unused:
        raise TemplateError(f"Values the template does not use: {unused}")
    if PLACEHOLDER.search(json.dumps(spec)):
        raise TemplateError("A value introduced a new placeholder; nothing was rendered.")
    return spec
