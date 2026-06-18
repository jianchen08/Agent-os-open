import sys, traceback
sys.path.insert(0, "src")
with open("_chk_out.txt", "w", encoding="utf-8") as f:
    try:
        from tools.builtin.web_search_mcp.tool import WebSearchMCPTool, web_search_mcp
        f.write("import OK\n")
        f.write(f"WebSearchMCPTool={WebSearchMCPTool}\n")
        f.write(f"web_search_mcp={web_search_mcp}\n")
        # 模拟 register_core_tools
        from tools.registry import ToolRegistry
        from tools.builtin import register_core_tools
        r = ToolRegistry()
        register_core_tools(r)
        f.write(f"has web_search: {r.has('web_search')}\n")
        if r.has("web_search"):
            h = r.get_handler("web_search")
            f.write(f"handler: {h}\n")
    except Exception as e:
        f.write(f"ERROR: {e}\n{traceback.format_exc()}\n")
