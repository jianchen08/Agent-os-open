import asyncio
import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, project_root)
os.chdir(project_root)


async def test_browser_navigate_and_screenshot():
    from src.tools.mcp_loader import MCPToolLoader, MCPServerConfig
    print("Step 1: Connect to Playwright MCP...")
    loader = MCPToolLoader()
    config = MCPServerConfig(
        name="pw",
        command="npx.cmd",
        args=["-y", "@playwright/mcp@latest"],
    )

    try:
        client = await asyncio.wait_for(loader._connect_server(config), timeout=90)
        print(f"  Connected! Tools available.")

        print("\nStep 2: Navigate to test page...")
        nav_result = await asyncio.wait_for(
            loader.call_tool(
                server_config=config,
                tool_name="browser_navigate",
                arguments={"url": "http://localhost:3456/dashboard-test.html"},
                timeout=30.0,
                overall_timeout=60.0,
            ),
            timeout=60,
        )
        print(f"  Navigation result type: {type(nav_result).__name__}")
        if isinstance(nav_result, dict):
            content = nav_result.get("content", [])
            for c in content[:3]:
                text = c.get("text", "")[:200] if isinstance(c, dict) else str(c)[:200]
                print(f"  Content: {text}")

        print("\nStep 3: Take screenshot...")
        ss_result = await asyncio.wait_for(
            loader.call_tool(
                server_config=config,
                tool_name="browser_take_screenshot",
                arguments={},
                timeout=30.0,
                overall_timeout=60.0,
            ),
            timeout=60,
        )
        print(f"  Screenshot result type: {type(ss_result).__name__}")
        if isinstance(ss_result, dict):
            content = ss_result.get("content", [])
            for c in content[:3]:
                ctype = c.get("type", "") if isinstance(c, dict) else ""
                if ctype == "image":
                    print(f"  Image captured! Size: {len(str(c.get('data', '')))} chars")
                else:
                    text = c.get("text", "")[:200] if isinstance(c, dict) else str(c)[:200]
                    print(f"  Content: {text}")

        print("\nAll steps completed successfully!")

    except asyncio.TimeoutError:
        print("  TIMEOUT!")
    except Exception as e:
        import traceback
        print(f"  FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        await loader.disconnect_all()


if __name__ == "__main__":
    asyncio.run(test_browser_navigate_and_screenshot())
