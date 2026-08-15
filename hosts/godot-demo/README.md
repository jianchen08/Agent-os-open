# AgentOS Godot Demo 工程

用于验证灵汐 AgentOS 与 Godot 4 编辑器之间的宿主连接链路（T8）：

```
Godot 4 编辑器内 agentos 插件（HTTP 127.0.0.1:9600）
   ↑ 探活/上下文/场景/选中/执行
game_engine 连接器（plugins/shared/system/connectors/creative/game_engine.py）
```

选中引用（事件驱动，无轮询）：编辑器选中节点 → 插件 POST 推送
`http://127.0.0.1:9100/ext/pipeline_godot_context/selection` → AgentOS
聊天框出现引用卡片，发送消息时引用随消息注入（详见 `hosts/godot-addon/README.md`）。

演示场景 `demo_main.tscn` 中 Player（Sprite2D）挂了测试贴图
`assets/player_icon.png`，可用于验证预览缩略图。

## 运行

需要 Godot 4.4+（宿主插件使用 `StreamPeer.get_partial_data`，该 API 自 4.4 起替代 `get_partial`）。
已在 4.7.1 上实测通过。

```bash
# 打开编辑器（插件启用后自动在 9600 端口启动宿主服务）
godot --editor --path hosts/godot-demo
```

验证：

```bash
curl http://127.0.0.1:9600/health
# -> {"status":"ok","version":"0.2.0"}

curl http://127.0.0.1:9600/status
curl http://127.0.0.1:9600/context
curl http://127.0.0.1:9600/scene
```

## 在自己的工程里使用

把 `addons/agentos/`（plugin.cfg + agentos_connector.gd，与 `hosts/godot-addon/` 同源）
复制到你的 Godot 4 工程 `addons/agentos/` 下，然后在
**项目 → 项目设置 → 插件** 中勾选启用 AgentOSConnector 即可。

端口/地址可在 Project Settings → AgentOS/Host 下覆盖（默认 127.0.0.1:9600）。
