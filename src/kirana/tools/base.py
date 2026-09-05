"""Tool definition and decorator.

Decoupled from external provider SDKs: defines a clean, inspectable KiranaTool
with name, description, input_schema, and async handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class KiranaTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[dict]]

    def __call__(self, *args, **kwargs):
        return self.handler(*args, **kwargs)


def tool(name: str, description: str, input_schema: dict | None = None):
    """Decorator to declare a store tool with name, docstring/description, and schema."""
    schema = input_schema or {}

    def decorator(fn: Callable[[dict], Awaitable[dict]]) -> KiranaTool:
        return KiranaTool(
            name=name,
            description=description.strip(),
            input_schema=schema,
            handler=fn,
        )

    return decorator
