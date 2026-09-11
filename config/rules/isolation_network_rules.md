## 隔离容器网络规则

> 适用场景：在隔离容器内启动 server、跑浏览器测试（playwright/e2e）。
> 根因见 system-issue #3：容器内命令与宿主浏览器分属不同网络命名空间，
> 容器里的 `127.0.0.1` ≠ 宿主的 `localhost`，"同 URL 一边 200 一边 REFUSED"。

### 核心原则：server 与浏览器测试必须在同一侧

**首选：都在容器内（同处一个网络命名空间，天然互通）**

- agent 通过 `bash_execute` 在容器内启动 server，并在**同一个容器内**用 playwright/pytest 跑测试
- 二者都打 `127.0.0.1:<端口>`，因为它们在同一个网络命名空间，`127.0.0.1` 指向同一个 loopback
- 容器镜像已预装 playwright + chromium + nodejs，可直接：
  - Python：`python -m pytest` / `python -c "from playwright.sync_api import ..."`
  - 前端：`npx playwright test`（需容器内先 `npm install`）
- **容器内 playwright 启动浏览器**：镜像装了完整 chromium（Chrome for Testing）。
  若 `chromium.launch()` 报「找不到 headless shell」，指定完整 chromium：
  ```python
  import glob
  chrome = glob.glob('/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome')[0]
  browser = p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
  ```

### 启动 server 时的绑定地址

- **容器内自测（首选）**：server 绑 `0.0.0.0` 或 `127.0.0.1` 均可，浏览器测试也在容器内即可
  - 例：`uvicorn main:app --host 0.0.0.0 --port 8000`
  - 例：`flask run --host 0.0.0.0 --port 5000`
- **需被宿主/外部访问时**：server **必须绑 `0.0.0.0`**，绝不能只绑 `127.0.0.1`
  - 原因：`127.0.0.1` 是容器自己的 loopback，宿主（含宿主上的浏览器工具）打不到
  - 即使配了端口映射，server 绑 `127.0.0.1` 时宿主依然 REFUSED（已验证）

### 禁止的反模式

- ❌ 容器内起 server（绑 `127.0.0.1`）+ 用宿主的 `browser_search`/`playwright_test` 工具去访问
  → 宿主浏览器打 `localhost:端口` 会 ERR_CONNECTION_REFUSED
- ❌ 假设容器里的 `localhost` 等于宿主的 `localhost` —— 它们是两个不同的 loopback
- ❌ 在容器里探测宿主服务时写 `localhost` —— bridge 网络下要用 `host.docker.internal`

### 诊断网络问题的工具（镜像已预装）

- `ss -ltnp`：查看监听端口（看 server 到底绑在哪个地址）
- `netstat -ltnp`：同上（备选）
- `ps aux`：查看进程（确认 server 是否真的在跑）
- `curl http://127.0.0.1:<端口>/`：容器内自测 server 是否可达

## 容器→宿主服务通路（MCP Bridge 网关，ADR 2026-09-10）

浏览器等工具的调用在隔离任务下由**容器内发起**（沙箱内 MCP Client），
目标是宿主机上的 MCP Bridge 网关（`scripts/start_bridge.bat`，默认 8765）。
容器侧通路已由 docker_provider 自动注入，无需手工配置：

- **地址候选**（bridge_client 静默自试，顺序）：
  1. `AGENTOS_BRIDGE_URL` 环境变量（docker create 时 `-e` 注入：config
     `bridge_url` > 宿主 env > WSL `ip route` 网关探测）
  2. `http://host.docker.internal:8765`（docker_provider 已注入
     `--add-host=host.docker.internal:host-gateway`，WSL 原生 docker 也可解析）
  3. WSL 网关 IP（如 `http://172.20.0.1:8765`）
- **认证**：`AGENTOS_BRIDGE_TOKEN`（docker create 时注入）；未配置时
  Bridge 拒绝一切调用（fail-closed）。
- **反模式**：容器内探 Bridge 写 `localhost:8765` —— 那是容器自己的
  loopback，连不到宿主（同上核心原则）。
- 审计：每次调用落 `logs/mcp-bridge/audit-YYYYMMDD.jsonl`（caller 字段
  host|sandbox 标识执行域）。
