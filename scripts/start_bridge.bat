@echo off
rem Start MCP Bridge gateway (host-side MCP-over-HTTP gateway for sandbox agents)
rem Config: config\bridge\bridge.yaml   Token env: AGENTOS_BRIDGE_TOKEN
rem ASCII-only rule: keep this file free of non-ASCII characters.

cd /d "%~dp0.."

if "%AGENTOS_BRIDGE_TOKEN%"=="" (
  echo [mcp-bridge] AGENTOS_BRIDGE_TOKEN is not set. Refusing to start without a token.
  echo Set it first, e.g.:  set /p AGENTOS_BRIDGE_TOKEN=<nul ^& set AGENTOS_BRIDGE_TOKEN=your-secret
  exit /b 1
)

echo [mcp-bridge] starting on 0.0.0.0:8765 ...
python mcp-servers\mcp-bridge\server.py