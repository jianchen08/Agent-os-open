# deploy/remote — F-min v0：云脑调度本机算力（部署配方）

> 决策：`docs/decisions/2026-10-12-fmin-remote-execution-via-mcp.md`。
> 形态：云机 7×24 内核（大脑）经 SSH 反向隧道调用本机 MCP server（算力）；
> 工作空间 = 本机 allowed_roots 真实目录，路径零改写（保真不变量）。

```
云机（唯一脑+库）                          本机（算力）
┌──────────────────────────┐   ssh -R（autossh 常驻）  ┌──────────────────────────┐
│ 内核 mcp:external         │  127.0.0.1:9201 ────────▶ │ fmin_local_server.py      │
│  manifest url=127.0.0.1   │  127.0.0.1:2222 ────────▶ │  :9201 MCP（Bearer）      │
│  ①工具面：tools/call 走隧道 │                          │  :22 sshd（供 sshfs 只读）  │
│  ②只读读面：sshfs ro ───────────────────────────────▶ │ allowed_roots 白名单目录    │
└──────────────────────────┘                           └──────────────────────────┘
```

| 端口/路径 | 归属 | 说明 |
|---|---|---|
| 9201 | MCP server（本机）/ 隧道 -R / manifest url（云） | 全程只存在于两侧 127.0.0.1，不经公网 |
| 2222 | 云机环回 → 本机 sshd | 仅 sshfs 只读挂载用；不用可去掉该转发 |
| `${AGENTOS_FMIN_LOCAL_TOKEN}` | 两端共享 | 本机 server 校验；云内核 connect 时解析进 `Authorization: Bearer` |

本目录五件：

| 文件 | 侧 | 用途 |
|---|---|---|
| `local_mcp_server/` | 本机 | MCP server v0（五工具 + 白名单 + Bearer + 审计日志） |
| `agentos-fmin-tunnel.service` | 本机 | autossh 反向隧道 systemd 模板 |
| `agentos-fmin-local-ro.service` | 云 | sshfs 只读挂载 systemd 模板（参数占位待 ADR 细化） |
| `mcp_local_host.plugin.json` | 云 | mcp:external manifest 模板 |
| 本 README | — | 部署三段 + 验收 |

---

## §1 本机侧

1. **装依赖**（Python ≥3.10；本批在 3.14 + mcp 2.0.0 上验证）：

   ```bash
   pip install "mcp>=2.0" uvicorn
   ```

2. **配置**：复制 `local_mcp_server/config.example.json` 为同目录 `config.json`，
   改 `allowed_roots`（绝对路径、必须已存在；这就是暴露给云内核的全部领地，
   云脑的文件/shell 操作出此范围一律被拒并报明确错误）。`audit_log` 是保真
   取证日志，建议保持仓内 `logs/` 相对路径。

3. **token**（只走环境变量，不落盘）：

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   export AGENTOS_FMIN_LOCAL_TOKEN=<上一步输出>
   ```

4. **启动**：

   ```bash
   cd deploy/remote/local_mcp_server
   python fmin_local_server.py --config config.json
   # 监听 127.0.0.1:9201；无 token 拒启（fail-closed）
   ```

   常驻：Linux/WSL 用 `agentos-fmin-tunnel.service` 旁的同类 unit 包一层即可
   （或 `systemd-run --user`）；本组件纯 Python 跨平台，Windows 原生 python 亦可。

5. **隧道**：装 autossh，按 `agentos-fmin-tunnel.service` 头注释部署
   （占位：`<cloud-user>@<cloud-host>`、`<local-user>`）。Windows 本机用 WSL
   systemd 或任务计划程序跑等价 `ssh -N -R ...`（模板头注释有说明）。

## §2 云侧

1. **manifest**：复制 `mcp_local_host.plugin.json` 到云内核插件目录
   `plugins/shared/mcp_local_host/plugin.json`。热发现自动装载，无需重启。
   - `url` 指云机 127.0.0.1:9201（隧道口）；环回在内核出网守卫白名单内，无需改守卫配置。
   - `auth.value` 的 `${AGENTOS_FMIN_LOCAL_TOKEN}` 由内核 connect 时解析——把
     token 写入内核环境（`deploy/agentos.env` 同源处），缺占位变量 + `required:true`
     = 连接失败早暴露（fail-closed）。
   - shell 长任务可给 manifest 追加 `"request_timeout_secs"`（内核机制，声明即契约）。

2. **只读挂载（可选，云侧读面）**：按 `agentos-fmin-local-ro.service` 部署，
   每个 allowed_root 一个实例。仅服务云侧预览/加载器/索引——**执行与写永不走
   挂载**；keepalive/timeout 参数基线待 ADR 细化（设计文档 §六.9）。

## §3 验收

前置：本机 server 与隧道已起（云机 `curl 127.0.0.1:9201` 有响应即隧道通）。

1. **通道与鉴权**：

   ```bash
   # 云机上执行：无 token 必须 401（未授权拒绝）
   curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:9201/mcp
   ```

2. **工具面发现**：云内核插件面出现 `mcp_local_host`，agent 会话里可见五工具
   `read_file / write_file / list_directory / search_files / run_shell`。

3. **路径保真锚（核心验收）**：让 agent 在 allowed_roots 内的工作空间路径上做
   **读/写/bash 三类操作**（如读 `D:\myproject\x.txt`、写一个文件、
   `run_shell echo`），然后在本机取证：

   ```bash
   python - <<'EOF'
   import json, sys
   bad = 0
   for line in open("logs/fmin_local_mcp_audit.jsonl", encoding="utf-8"):
       e = json.loads(line)
       if e["tool"] in ("read_file", "write_file", "run_shell") and e.get("ok"):
           if e["model_path"] != e["executed_path"]:
               bad += 1
               print("不一致：", e)
   print("路径保真锚：diff 为空" if not bad else f"FAIL：{bad} 条不一致")
   sys.exit(1 if bad else 0)
   EOF
   ```

   判定：`model_path`（模型上下文里的路径字符串）与 `executed_path`（实际交给
   OS 的路径）**逐字节一致**，且与 agent 会话里所写路径一致——全链路零改写点
   成立。再用 `list_directory` 交叉确认文件真实落在该路径。

4. **越界拒绝**：让 agent 读一个 allowed_roots 外路径（如 `C:\Windows\win.ini`
   或白名单父目录），必须得到明确"路径越界"报错而非静默失败。

5. **只读挂载面**（若启用）：云机 `cat /mnt/fmin/<挂载点名>/某文件` 与本机原文
   一致；云机侧尝试写挂载点必须失败（ro）。

## 本机测试车道

```bash
python -m pytest deploy/remote/local_mcp_server/tests -p no:cacheprovider
```

覆盖三场景（正常调用/越界拒绝/未授权 401）+ 路径保真锚断言，走真实 MCP
streamable-http 协议栈。
