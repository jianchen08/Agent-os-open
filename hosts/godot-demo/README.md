# AgentOS Godot Demo 工程

用于验证灵汐 AgentOS 与 Godot 4 编辑器之间的宿主连接链路，双 addon 并存：

```
addons/agentos/    选中引用桥（事件驱动）：9600 HTTP + 推送内核 9100
addons/godot_mcp/  执行面（godot-mcp-go v0.9.0）：9080 WebSocket + 9180 编辑器直连 HTTP
```

## 选中引用桥（addons/agentos）

编辑器选中节点 → 插件 POST 推送
`http://127.0.0.1:9100/ext/pipeline_godot_context/selection` → AgentOS
聊天框出现引用卡片，发送消息时引用随消息注入 `<reference source="godot">`
（详见 `hosts/godot-addon/README.md`）。心跳 5s，15s 无心跳视为离线。

演示场景 `demo_main.tscn` 中 Player（Sprite2D）挂了测试贴图
`assets/player_icon.png`，可用于验证预览缩略图。

## 执行面（addons/godot_mcp）

AgentOS 侧经外部 MCP 插件 `plugins/shared/tools/external_mcp/godot_mcp/`
（`godot_run` 单工具，stdio 连 `D:/myproject/godot-mcp-go/bin/godot-mcp.exe
serve --typed=false --project <本工程>`）驱动编辑器：建场景/写脚本/
运行/调试/导出（332 命令，`engine.search` 可发现 API）。编辑器未开时工具
返回 `editor_unreachable` 与恢复指引。

两 addon 配合：`<reference>` 给出用户所选节点，`godot_run` 以这些节点
路径为操作锚点。

端口注意：addon 编辑器直连 HTTP 默认在 9100-9115 扫端口，会撞 AgentOS
内核（9100，Windows 特定绑定可越过 0.0.0.0 绑定抢走本机流量）——
`project.godot [godot_mcp] network/http_port=9180` 已钉死避开。

## 运行

需要 Godot 4.4+（agentos 插件使用 `StreamPeer.get_partial_data`，该 API 自
4.4 起替代 `get_partial`）；godot_mcp addon 目标 4.7。已在 4.7.1 上实测通过。

```bash
# Windows 一键启动（自动定位 winget 安装的 Godot）
start_godot_demo.bat

# 或手动
godot --editor --path hosts/godot-demo
```

验证：

```bash
curl http://127.0.0.1:9600/health
# -> {"status":"ok","version":"0.2.0"}

curl http://127.0.0.1:9600/context     # 当前场景/选中
curl http://127.0.0.1:9180/mcp         # godot_mcp 编辑器直连 HTTP（POST JSON-RPC）

# AgentOS 侧选中快照（内核须在跑，登录后带 Bearer）
curl http://127.0.0.1:9100/ext/pipeline_godot_context/selection -H "Authorization: Bearer <token>"
```

## 在自己的工程里使用

- 选中引用：把 `addons/agentos/` 复制到你的 Godot 4 工程 `addons/agentos/`，
  项目设置 → 插件中启用 AgentOSConnector。端口/地址可在
  Project Settings → AgentOS/Host 下覆盖（默认 127.0.0.1:9600）。
- 执行面：把 `addons/godot_mcp/` 一并复制并启用，AgentOS 侧把
  `godot_mcp` 插件 manifest 里 `--project` 改指向你的工程；
  `network/http_port` 按需避开内核 9100。
