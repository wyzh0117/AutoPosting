from __future__ import annotations

import json
from typing import Any

from .paths import example_path, schema_path

KIND_BY_PAGE = {
    "ranking-overview": "ranking",
    "project-detail": "project",
}

# Lane text: fields a non-weekly lane may intentionally blank because it does not
# want GitHub weekly branding (``issue=""`` suppresses the ISO week label). An
# empty string stays legal for these keys only; missing keys still fail
# ``required``, and every other string field keeps enforcing ``minLength``.
# See ``adapter._lane_text``.
BLANKABLE_LANE_KEYS = frozenset({"brand", "series", "issue", "dateRange", "title", "subtitle"})


class ContentValidationError(ValueError):
    pass


def load_example(kind: str) -> dict[str, Any]:
    path = example_path(kind)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContentValidationError(f"example is not an object: {path}")
    return payload


def validate_content(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(schema_path(kind).read_text(encoding="utf-8"))
    _check(payload, schema, schema, "$")
    return payload


def infer_kind(payload: dict[str, Any]) -> str:
    page = str(payload.get("page") or "")
    if page not in KIND_BY_PAGE:
        raise ContentValidationError(f"unknown page: {page or '(missing)'}")
    return KIND_BY_PAGE[page]


def _check(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str, key: str = "") -> None:
    schema = _resolve(schema, root)
    if "const" in schema and value != schema["const"]:
        raise ContentValidationError(f"{path}: expected {schema['const']!r}")
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        raise ContentValidationError(f"{path}: expected {expected}")
    if expected == "object" or (isinstance(value, dict) and "properties" in schema):
        if not isinstance(value, dict):
            raise ContentValidationError(f"{path}: expected object")
        for key_name in schema.get("required") or []:
            if key_name not in value:
                raise ContentValidationError(f"{path}: missing {key_name}")
        properties = schema.get("properties") or {}
        for key_name, child in value.items():
            if key_name in properties:
                _check(child, properties[key_name], root, f"{path}.{key_name}", key_name)
    if expected == "array" or "items" in schema:
        if not isinstance(value, list):
            if expected == "array":
                raise ContentValidationError(f"{path}: expected array")
            return
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ContentValidationError(f"{path}: minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _check(item, item_schema, root, f"{path}[{index}]")
    if isinstance(value, str) and "minLength" in schema and len(value) < int(schema["minLength"]):
        if value == "" and key in BLANKABLE_LANE_KEYS:
            return
        raise ContentValidationError(f"{path}: minLength {schema['minLength']}")
    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        raise ContentValidationError(f"{path}: minimum {schema['minimum']}")


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not str(ref).startswith("#/$defs/"):
        raise ContentValidationError(f"unsupported $ref: {ref}")
    name = str(ref).split("/")[-1]
    defs = root.get("$defs") if isinstance(root.get("$defs"), dict) else {}
    target = defs.get(name)
    if not isinstance(target, dict):
        raise ContentValidationError(f"unknown $ref: {ref}")
    return target


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True
