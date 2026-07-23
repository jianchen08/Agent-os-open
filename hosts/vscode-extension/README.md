# AgentOS Host - VSCode 扩展

灵汐 AgentOS（v0.2）的 VSCode 宿主接入 Demo。

本扩展作为 **对端**（peer），在 VSCode 编辑器内启动一个最小 HTTP 服务，供仓内
`plugins/shared/system/connectors/vscode/` 下的连接器轮询调用：

- 获取 VSCode 当前上下文（活动文件 / 选中文本 / 光标位置 / 打开文件列表）
- 向 VSCode 下发操作（打开文件 / 插入内容 / 显示差异）

> 连接器侧（Python）无需改动，已通过 HTTP 短轮询连接 `127.0.0.1:9741`。

## 端点

| 方法   | 路径           | 说明                                                                 |
| ------ | -------------- | -------------------------------------------------------------------- |
| GET    | `/health`      | 探活，返回 `200 { "status": "ok", "version": "0.2.0" }`              |
| GET    | `/capabilities`| 返回支持的能力列表                                                   |
| POST   | `/context`     | 返回上下文（字段对齐 `channel.py::_parse_context`）                  |
| POST   | `/action`      | 执行动作，body: `{ action_type, parameters, action_id }`             |

### `/context` 返回字段

```jsonc
{
  "active_file": "D:\\proj\\main.py",            // 当前活动文件路径，无则 null
  "selected_text": "print(1)",                    // 选中文本，无选中为 null
  "cursor_position": { "line": 0, "column": 0 },  // 光标行列（均从 0 开始）
  "open_files": ["D:\\proj\\main.py", "..."],     // 可见编辑器的文件列表
  "metadata": { "language_id": "python", "workspace": "...", "extension_version": "0.2.0" }
}
```

### `/action` 支持的 `action_type`

| action_type     | parameters                                            | 行为                     |
| --------------- | ----------------------------------------------------- | ------------------------ |
| `open_file`     | `{ "file": "<abs path>" }`                            | 打开指定文件             |
| `insert_content`| `{ "content": "...", "file"?: "...", "position"?: {line,column} }` | 插入文本，缺省用当前光标 |
| `show_diff`     | `{ "left": "<abs path>", "right": "<abs path>", "label"?: "..." }` | 调用 `vscode.diff`       |

返回：`{ "success": true|false, "data"?: any, "error"?: "..." }`

## 安装与运行

### 方式一：开发调试（推荐用于 Demo）

```bash
cd hosts/vscode-extension
npm install          # 安装 devDependencies（@types/vscode、typescript、@types/node）
npm run compile      # 编译 extension.ts -> out/extension.js
```

然后用 VSCode 打开 `hosts/vscode-extension/` 目录，按 `F5` 启动扩展开发宿主
（Extension Development Host）。扩展激活后会自动监听 `127.0.0.1:9741`。

> 也可不安装依赖，直接人工检查 `extension.ts` 语法（仅使用 Node 内置 `http` 与
> `vscode` API，无第三方运行时依赖）。

### 方式二：打包安装

```bash
cd hosts/vscode-extension
npm install
npm run compile
npx @vscode/vsce package     # 生成 agentos-vscode-host-0.2.0.vsix
code --install-extension agentos-vscode-host-0.2.0.vsix
```

## 配置项

| 配置项             | 默认值       | 说明                                   |
| ------------------ | ------------ | -------------------------------------- |
| `agentosHost.port` | `9741`       | HTTP 服务监听端口（需与连接器一致）    |
| `agentosHost.host` | `127.0.0.1`  | HTTP 服务监听地址，默认仅本机访问      |

## 命令

- `AgentOS Host: 启动服务` (`agentosHost.start`)
- `AgentOS Host: 停止服务` (`agentosHost.stop`)

## 验证

扩展激活后可手动验证：

```bash
curl http://127.0.0.1:9741/health
# -> {"status":"ok","version":"0.2.0"}

curl -X POST http://127.0.0.1:9741/context
# -> { ... 当前上下文 ... }
```

## 与灵汐连接器的关系

```
┌──────────────┐  HTTP 轮询 (9741)   ┌─────────────────────────┐
│ 灵汐 Python  │  <───────────────── │ 本扩展 (VSCode 内)       │
│ vscode 连接器 │  /health /context   │ Node http server        │
│ (channel.py) │  /action            │                          │
└──────────────┘                     └─────────────────────────┘
```

- 连接器（Python）：`plugins/shared/system/connectors/vscode/connector.py`、`channel.py`
- 本扩展（对端）：`hosts/vscode-extension/extension.ts`
