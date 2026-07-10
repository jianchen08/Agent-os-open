## 问题根因(已实测确认)

前端容器(`agent-os-frontend-22404`)连不上后端。完整证据链:

1. **后端**跑在 Windows 宿主 `0.0.0.0:8988`(start_web_cn.bat:394,实测 `curl 127.0.0.1:8988/health` → 200,2ms)✅
2. **前端**跑在 WSL docker 网络 `172.18.0.3`(WSL 原生 docker 模式,非 Docker Desktop)
3. compose 配的 `BACKEND_URL=http://host.docker.internal:8988`,但 **WSL 原生 docker 不注入 `host.docker.internal` DNS**(实测容器内 `socket.gaierror: Name or service not known`)
4. `frontend/server.py:127` 用此 URL 连后端 → `httpx.ConnectError` → 捕获返回 `{"detail":"后端服务不可达"}` 502(每次固定 3.9s,即连接超时)

**已验证的可行路径:** 容器能访问 `172.29.128.1:8988`(Windows 宿主的 WSL vEthernet 网关,实测 200,2.8ms)。无需改防火墙(防火墙已关闭)或加 portproxy(那是反方向)。

**与用户贴的构建警告无关:** `INEFFECTIVE_DYNAMIC_IMPORT` / chunk 过大是 Vite 生产构建的代码分割提示;`client.ts` 的动态 import 是刻意设计(打破循环依赖),不改。

## 修复方案:用 `extra_hosts: host-gateway` 让容器稳定访问宿主

docker 内置 `host-gateway` 关键字(会自动解析为宿主网关地址,IP 变化也能跟上),比硬编码 IP 稳定。改动两处:

### 改动 1: `docker-compose.yml`(frontend 服务)
- 第 29-30 行的 `BACKEND_URL`/`BACKEND_WS_URL` 保持 `host.docker.internal:8988`(不改,语义正确)
- 新增 `extra_hosts`,把 `host.docker.internal` 显式映射到 `host-gateway`:
  ```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
  ```
  这样无论 Docker Desktop 还是 WSL 原生 docker,容器都能解析 `host.docker.internal` → 宿主网关。

### 改动 2: `start_web_cn.bat`(WSL native docker 分支)
脚本第 159-160 行在 `containers_ok` 后会重建/更新前端代码。为保险,确保容器用新 compose 配置重建。无需改脚本逻辑本身——重启容器加载新 compose 即可(改动在 compose 层,容器重建会自动生效)。

### 不改动
- `frontend/server.py`:逻辑正确,`BACKEND_URL` 环境变量已支持,不改
- `frontend/.env` / `.env.local`:那是 Vite dev server 用的,与 server.py 无关,不改
- `client.ts` 的动态 import:刻意设计,不改

## 实施步骤
1. 编辑 `docker-compose.yml`:在 frontend 服务下加 `extra_hosts: ["host.docker.internal:host-gateway"]`
2. 重建 frontend 容器使配置生效:`docker compose up -d --force-recreate frontend`(需在计划批准后执行)
3. 验证:容器内 `python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"` 应解析成功;`curl http://127.0.0.1:5289/api/v1/agents/health` 应返回 401(而非 502)

## 回归验证标准
- `curl http://127.0.0.1:5289/api/v1/agents/health` → 401(连上后端,只是缺 token),不再是 502
- `curl http://127.0.0.1:5289/api/v1/auth/me` → 401
- 浏览器打开 http://127.0.0.1:5289,页面正常加载不再报连接错误