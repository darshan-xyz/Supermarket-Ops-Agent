"""Aggregate every domain tool list into tool mappings and Gemini tool declarations.

Tools are callable directly by their names (e.g. `add_product`, `cart_add_item`).
"""

from __future__ import annotations

from typing import Any
from google.genai import types

from .analytics import ANALYTICS_TOOLS
from .billing import BILLING_TOOLS
from .customer import CUSTOMER_TOOLS
from .documents import DOCUMENT_TOOLS
from .employee import EMPLOYEE_TOOLS
from .inventory import INVENTORY_TOOLS
from .khata import KHATA_TOOLS
from .preferences import MEMORY_TOOLS
from .base import KiranaTool

ALL_TOOLS: list[KiranaTool] = [
    *INVENTORY_TOOLS,
    *BILLING_TOOLS,
    *CUSTOMER_TOOLS,
    *KHATA_TOOLS,
    *ANALYTICS_TOOLS,
    *DOCUMENT_TOOLS,
    *MEMORY_TOOLS,
    *EMPLOYEE_TOOLS,
]

TOOLS_BY_NAME: dict[str, KiranaTool] = {t.name: t for t in ALL_TOOLS}


def _json_schema_to_gemini_schema(s: dict | Any) -> types.Schema:
    """Convert JSON Schema dictionary to Google GenAI types.Schema."""
    if not isinstance(s, dict):
        # Support primitive types if specified directly
        if s is int:
            return types.Schema(type=types.Type.INTEGER)
        if s is float:
            return types.Schema(type=types.Type.NUMBER)
        if s is str:
            return types.Schema(type=types.Type.STRING)
        if s is bool:
            return types.Schema(type=types.Type.BOOLEAN)
        return types.Schema(type=types.Type.OBJECT)

    t_map = {
        "string": types.Type.STRING,
        "integer": types.Type.INTEGER,
        "number": types.Type.NUMBER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
        "object": types.Type.OBJECT,
    }
    stype = t_map.get(s.get("type", "object"), types.Type.OBJECT)
    kw: dict[str, Any] = {"type": stype}
    if "description" in s:
        kw["description"] = s["description"]
    if "enum" in s:
        kw["enum"] = [str(e) for e in s["enum"]]
    if "properties" in s:
        props = {}
        for k, v in s["properties"].items():
            props[k] = _json_schema_to_gemini_schema(v)
        kw["properties"] = props
    if "required" in s:
        kw["required"] = list(s["required"])
    if "items" in s:
        kw["items"] = _json_schema_to_gemini_schema(s["items"])
    return types.Schema(**kw)


def build_gemini_tools() -> list[types.Tool]:
    """Convert all domain tools into Gemini FunctionDeclarations wrapped in a types.Tool."""
    decls = []
    for t in ALL_TOOLS:
        schema = _json_schema_to_gemini_schema(t.input_schema)
        decl = types.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters=schema,
        )
        decls.append(decl)
    return [types.Tool(function_declarations=decls)]


def tool_names() -> list[str]:
    return [t.name for t in ALL_TOOLS]

