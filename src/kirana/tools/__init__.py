"""Agent tools. Each domain module (inventory, billing, khata, analytics,
documents, preferences) exposes a list of `@tool` objects; `registry.py`
aggregates them into a single in-process MCP server named ``kirana``.
"""
