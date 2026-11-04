"""A deliberately tiny JSON Schema validator.

Supports the subset our agent schemas use: type (incl. unions), properties, required,
additionalProperties=false, enum, items. Avoids pulling `jsonschema` (+ compiled deps) into the
layer. Providers with strict structured outputs already guarantee shape; this is the
provider-independent backstop (FakeLLM and tool-use responses are not strictly enforced).
"""

from __future__ import annotations

from typing import Any

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    pass


def _type_ok(value: Any, expected: str) -> bool:
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _PY_TYPES[expected])


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in options):
            raise SchemaError(f"{path}: expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required property '{key}'")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected properties {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")
