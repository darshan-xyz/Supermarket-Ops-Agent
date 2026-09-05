"""Verify Gemini tool schema translation and registry."""

from kirana.tools.registry import ALL_TOOLS, TOOLS_BY_NAME, build_gemini_tools
from google.genai import types

def test_all_tools_in_registry():
    assert len(ALL_TOOLS) >= 30
    assert len(TOOLS_BY_NAME) == len(ALL_TOOLS)

def test_gemini_tools_builder():
    gemini_tools = build_gemini_tools()
    assert len(gemini_tools) == 1
    tool_obj = gemini_tools[0]
    assert isinstance(tool_obj, types.Tool)
    decls = tool_obj.function_declarations
    assert len(decls) == len(ALL_TOOLS)
    
    decl_names = {d.name for d in decls}
    assert "add_product" in decl_names
    assert "cart_add_item" in decl_names
    assert "cart_finalize" in decl_names
    assert "khata_add_credit" in decl_names
    assert "generate_invoice_pdf" in decl_names

    get_stock_decl = next(d for d in decls if d.name == "get_stock")
    assert get_stock_decl.parameters is not None
    assert "product_id" in get_stock_decl.parameters.properties
    assert "product_id" in get_stock_decl.parameters.required
