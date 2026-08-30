"""Strict parsing of the JSON a judge model returns.

A lenient parser is a measurement hazard: text the model meant as prose,
read as a verdict, becomes a number in a table. Everything here fails loudly
so the caller can retry once and then report ``INCOMPLETE`` (EVAL-4).
"""

from __future__ import annotations

import json
from typing import Any

_FENCE = "```"


class ResponseFormatError(Exception):
    """The model's response was not the JSON array the prompt asked for."""


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array of objects from a model response.

    Tolerates a code fence and surrounding prose, because open models emit
    both. Tolerates nothing about the shape: the payload must be an array,
    and every element must be an object.
    """
    payload = _isolate_array(text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ResponseFormatError(f"response is not valid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise ResponseFormatError(f"expected a JSON array, got {type(parsed).__name__}")

    for position, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ResponseFormatError(
                f"array element {position} is {type(item).__name__}, expected an object"
            )
    return parsed


def require_keys(item: dict[str, Any], keys: tuple[str, ...], *, position: int) -> None:
    """Reject an element missing any required key."""
    missing = [key for key in keys if key not in item]
    if missing:
        raise ResponseFormatError(f"array element {position} is missing key(s) {missing}")


def _isolate_array(text: str) -> str:
    stripped = text.strip()
    if _FENCE in stripped:
        blocks = stripped.split(_FENCE)
        # Odd indices are fenced blocks; take the first that looks like an array.
        for block in blocks[1::2]:
            body = block.split("\n", 1)[-1] if block.lstrip().lower().startswith("json") else block
            if "[" in body:
                stripped = body.strip()
                break

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ResponseFormatError("no JSON array found in response")
    return stripped[start : end + 1]
