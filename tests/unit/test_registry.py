"""The extraction registry is the only thing you edit to add a field."""

import textwrap

import pytest
import yaml
from conftest import ROOT, FakeLambdaContext, load_function

from acme_common.llm import FakeLLM, set_llm_client
from acme_common.registry import RegistryError, load_registry, parse_registry

CTX = FakeLambdaContext()

TICKET = {
    "subject": "Search crashes",
    "body": "Search crashes with SRCH-5001 on macOS, app 4.12.1, using Firefox.",
    "customer_tier": "business",
}


def test_shipped_registry_is_valid_and_generates_strict_schema():
    registry = load_registry(str(ROOT / "config" / "extraction_registry.yaml"))
    schema = registry.json_schema()
    assert registry.names == ["product_area", "error_code", "os", "app_version"]
    assert schema["required"] == registry.names
    assert schema["additionalProperties"] is False
    assert schema["properties"]["os"]["enum"][-1] is None  # nullable enum
    assert "product_area [enum] (one of: editor" in registry.prompt_block()


def test_coercion_rejects_values_outside_enum_and_pattern():
    registry = load_registry(str(ROOT / "config" / "extraction_registry.yaml"))
    clean, warnings = registry.coerce(
        {"product_area": "Teleportation", "error_code": "sync-409", "os": "MacOS", "extra": 1}
    )
    assert clean == {"product_area": None, "error_code": None, "os": "macos", "app_version": None}
    assert set(warnings) == {"product_area", "error_code"}


@pytest.mark.parametrize(
    "doc",
    [
        {"fields": []},
        {"fields": [{"name": "Bad Name", "type": "string"}]},
        {"fields": [{"name": "a", "type": "enum"}]},
        {"fields": [{"name": "a", "type": "float"}]},
        {"fields": [{"name": "a"}, {"name": "a"}]},
        {"fields": [{"name": "a", "pattern": "("}]},
    ],
)
def test_invalid_registries_are_rejected(doc):
    with pytest.raises(RegistryError):
        parse_registry(doc)


def test_adding_a_field_is_config_only(tmp_path, monkeypatch):
    """Append `browser` to a copy of the registry and run the *unchanged* extractor."""
    doc = yaml.safe_load((ROOT / "config" / "extraction_registry.yaml").read_text())
    doc["fields"].append(
        yaml.safe_load(
            textwrap.dedent(
                """
                name: browser
                type: enum
                values: [chrome, firefox, safari, edge]
                description: Browser used for the web app, if mentioned.
                """
            )
        )
    )
    path = tmp_path / "extraction_registry.yaml"
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("EXTRACTION_REGISTRY_PATH", str(path))
    load_registry.cache_clear()

    fake = FakeLLM()
    set_llm_client(fake)
    try:
        out = load_function("extractor").lambda_handler({"ticket": TICKET}, CTX)
    finally:
        set_llm_client(None)
        load_registry.cache_clear()

    fields = out["output"]["fields"]
    assert fields["browser"] == "firefox"
    assert fields["error_code"] == "SRCH-5001"
    assert fields["os"] == "macos"
    assert fields["app_version"] == "4.12.1"
    assert fields["product_area"] == "search"
    # ...and the model was asked for it via the generated schema and prompt.
    assert "browser [enum]" in fake.calls[0]["system"]
