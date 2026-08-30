"""Dotted field paths over a Structured Clinical Reference.

A path addresses one field of the reference: ``core.diagnoses``,
``context.medical_history.past_medical_history``. A ``[]`` segment steps
through every element of a list of entities:
``core.treatments[].medications[].purpose``.

Paths are enumerated *from the model*, not hardcoded. Adding a field to
the SCR therefore adds it to :func:`policy_surface`, and every policy that
does not classify it fails validation — a new field cannot become visible
by default (KNOW-3).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, MutableMapping
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from meddial.knowledge.reference import StructuredClinicalReference

#: Returned by :func:`resolve` when a path addresses nothing in a payload.
MISSING: Any = object()

#: Never addressable by a policy: identifiers are carried as run metadata,
#: and evidence spans quote the source note verbatim.
_EXCLUDED_ROOT_FIELDS = frozenset({"row_id", "subject_id", "hadm_id"})
_EXCLUDED_FIELDS = frozenset({"evidence"})


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """The model behind a field annotation, if the field holds models."""
    annotation = _unwrap_optional(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) is list:
        (inner,) = get_args(annotation) or (None,)
        inner = _unwrap_optional(inner)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            return inner
    return None


def _is_list(annotation: Any) -> bool:
    return get_origin(_unwrap_optional(annotation)) is list


def addressable_paths(
    model: type[BaseModel] = StructuredClinicalReference,
    *,
    prefix: str = "",
) -> frozenset[str]:
    """Every path a policy is allowed to name, including element sub-paths."""
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        if name in _EXCLUDED_FIELDS:
            continue
        if not prefix and name in _EXCLUDED_ROOT_FIELDS:
            continue
        path = f"{prefix}{name}"
        paths.add(path)
        nested = _model_of(field.annotation)
        if nested is None:
            continue
        step = f"{path}[]." if _is_list(field.annotation) else f"{path}."
        paths |= addressable_paths(nested, prefix=step)
    return frozenset(paths)


def policy_surface(
    model: type[BaseModel] = StructuredClinicalReference,
) -> frozenset[str]:
    """The paths every policy must classify as visible or masked.

    One level below the top-level sections (``core``/``context``/
    ``additional``), so a policy decides about ``context.current_medications``
    as a whole rather than field by field.
    """
    surface: set[str] = set()
    for name, field in model.model_fields.items():
        if name in _EXCLUDED_ROOT_FIELDS or name in _EXCLUDED_FIELDS:
            continue
        section = _model_of(field.annotation)
        if section is None:
            surface.add(name)
            continue
        for sub in section.model_fields:
            if sub not in _EXCLUDED_FIELDS:
                surface.add(f"{name}.{sub}")
    return frozenset(surface)


def covering_surface(path: str) -> str | None:
    """The surface path that ``path`` falls under, or None if it is outside."""
    for candidate in policy_surface():
        if path == candidate or path.startswith((f"{candidate}.", f"{candidate}[")):
            return candidate
    return None


def _tokens(path: str) -> list[str]:
    return path.split(".")


def locations(
    payload: Any, path: str
) -> Iterator[tuple[MutableMapping[str, Any], str]]:
    """Yield ``(container, key)`` for every place ``path`` lands in a payload.

    A path with no ``[]`` yields at most one pair; a path through a list
    yields one per element. Used by both :func:`drop` and redaction so the
    two agree exactly on what a path covers.
    """
    tokens = _tokens(path)
    containers: list[Any] = [payload]
    for token in tokens[:-1]:
        listed = token.endswith("[]")
        key = token[:-2] if listed else token
        following: list[Any] = []
        for container in containers:
            if not isinstance(container, MutableMapping) or key not in container:
                continue
            value = container[key]
            if listed:
                if isinstance(value, list):
                    following.extend(v for v in value if isinstance(v, MutableMapping))
            elif isinstance(value, MutableMapping):
                following.append(value)
        containers = following
        if not containers:
            return
    last = tokens[-1]
    last_key = last.removesuffix("[]")
    for container in containers:
        if isinstance(container, MutableMapping) and last_key in container:
            yield container, last_key


def resolve(payload: Mapping[str, Any], path: str) -> Any:
    """The value at ``path``, or :data:`MISSING`.

    For a path containing ``[]`` the result is the list of values found,
    and :data:`MISSING` only when the path reaches nothing at all.
    """
    found = [container[key] for container, key in locations(payload, path)]
    if not found:
        return MISSING
    if "[]" in path:
        return found
    return found[0]


def _assign(target: MutableMapping[str, Any], path: str, value: Any) -> None:
    *parents, leaf = _tokens(path)
    cursor: MutableMapping[str, Any] = target
    for name in parents:
        nxt = cursor.setdefault(name, {})
        if not isinstance(nxt, MutableMapping):  # pragma: no cover - guarded by validation
            return
        cursor = nxt
    if isinstance(cursor.get(leaf), MutableMapping) and isinstance(value, Mapping):
        cursor[leaf].update(value)
    else:
        cursor[leaf] = value


def project(payload: Mapping[str, Any], paths: frozenset[str]) -> dict[str, Any]:
    """Build a new payload containing only ``paths``.

    An allowlist, not a filter: anything not named is absent, which is what
    makes an unclassified field invisible rather than exposed.
    """
    out: dict[str, Any] = {}
    # Shortest first so a parent path never overwrites a child already set.
    for path in sorted(paths, key=lambda p: (p.count("."), p)):
        value = resolve(payload, path)
        if value is MISSING:
            continue
        _assign(out, path, copy.deepcopy(value))
    return out


def drop(payload: MutableMapping[str, Any], path: str) -> int:
    """Remove ``path`` wherever it occurs. Returns how many were removed."""
    targets = list(locations(payload, path))
    for container, key in targets:
        container.pop(key, None)
    return len(targets)


def strip_evidence(value: Any) -> Any:
    """Remove every evidence span from a payload.

    Evidence quotes the source note verbatim, so it may never reach a
    participant context — only the evaluator holds it (C2, KNOW-7).
    """
    if isinstance(value, MutableMapping):
        value.pop("evidence", None)
        for nested in value.values():
            strip_evidence(nested)
    elif isinstance(value, list):
        for item in value:
            strip_evidence(item)
    return value
