"""Extraction registry: the fields the Extractor agent pulls out of a ticket.

The registry (config/extraction_registry.yaml) drives three things, so adding a field is a
config-only change:
  1. the JSON schema sent to the model (structured output),
  2. the field list in the prompt,
  3. post-validation/coercion of the model output (enum membership, regex patterns, types).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

FIELD_TYPES = {"string", "enum", "boolean", "integer"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_JSON_TYPE = {"string": "string", "enum": "string", "boolean": "boolean", "integer": "integer"}

# Layer build copies config/ next to this module; local runs fall back to the repo copy.
_PACKAGED = Path(__file__).parent / "config" / "extraction_registry.yaml"
_REPO = Path(__file__).resolve().parents[3] / "config" / "extraction_registry.yaml"


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    description: str
    values: tuple[str, ...] = ()
    pattern: str | None = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": [_JSON_TYPE[self.type], "null"],
            "description": self.description,
        }
        if self.type == "enum":
            schema["enum"] = [*self.values, None]
        return schema

    def prompt_line(self) -> str:
        hint = f" (one of: {', '.join(self.values)})" if self.values else ""
        return f"- {self.name} [{self.type}]{hint}: {self.description}"

    def coerce(self, value: Any) -> tuple[Any, str | None]:
        """Returns (clean_value, warning). Invalid values become None, with a warning."""
        if value is None:
            return None, None
        if self.type == "boolean":
            return (value, None) if isinstance(value, bool) else (None, "not a boolean")
        if self.type == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
            return (value, None) if ok else (None, "not an integer")
        if not isinstance(value, str):
            return None, "not a string"
        value = value.strip()
        if self.type == "enum":
            normalised = value.lower()
            return (
                (normalised, None)
                if normalised in self.values
                else (None, f"{value!r} not allowed")
            )
        if self.pattern and not re.fullmatch(self.pattern, value):
            return None, f"{value!r} does not match {self.pattern}"
        return value[:200], None


@dataclass(frozen=True)
class Registry:
    version: str
    fields: tuple[FieldSpec, ...]

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {f.name: f.json_schema() for f in self.fields},
            "required": self.names,
            "additionalProperties": False,
        }

    def prompt_block(self) -> str:
        return "\n".join(f.prompt_line() for f in self.fields)

    def coerce(self, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        clean: dict[str, Any] = {}
        warnings: dict[str, str] = {}
        for spec in self.fields:
            value, warning = spec.coerce(data.get(spec.name))
            clean[spec.name] = value
            if warning:
                warnings[spec.name] = warning
        return clean, warnings


def parse_registry(doc: Any) -> Registry:
    if not isinstance(doc, dict) or not isinstance(doc.get("fields"), list) or not doc["fields"]:
        raise RegistryError("registry must have a non-empty 'fields' list")
    seen: set[str] = set()
    fields: list[FieldSpec] = []
    for raw in doc["fields"]:
        name = (raw or {}).get("name", "")
        if not _NAME_RE.match(name):
            raise RegistryError(f"invalid field name {name!r} (snake_case, <= 40 chars)")
        if name in seen:
            raise RegistryError(f"duplicate field {name!r}")
        seen.add(name)
        ftype = raw.get("type", "string")
        if ftype not in FIELD_TYPES:
            raise RegistryError(f"{name}: type must be one of {sorted(FIELD_TYPES)}")
        values = tuple(str(v).lower() for v in raw.get("values") or ())
        if ftype == "enum" and not values:
            raise RegistryError(f"{name}: enum fields need 'values'")
        pattern = raw.get("pattern")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RegistryError(f"{name}: bad pattern: {exc}") from exc
        fields.append(
            FieldSpec(
                name=name,
                type=ftype,
                description=str(raw.get("description", "")).strip(),
                values=values,
                pattern=pattern,
            )
        )
    return Registry(version=str(doc.get("version", "1")), fields=tuple(fields))


def default_registry_path() -> Path:
    env = os.environ.get("EXTRACTION_REGISTRY_PATH")
    if env:
        return Path(env)
    return _PACKAGED if _PACKAGED.exists() else _REPO


@lru_cache(maxsize=4)
def load_registry(path: str | None = None) -> Registry:
    target = Path(path) if path else default_registry_path()
    with open(target, encoding="utf-8") as fh:
        return parse_registry(yaml.safe_load(fh))
