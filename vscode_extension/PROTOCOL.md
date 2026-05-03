# Agent OS VSCode Extension — 通信协议说明

## 概述

Agent OS VSCode 扩展通过 HTTP 短轮询与后端服务通信。扩展充当 HTTP 服务端（端口 9741），后端作为客户端发送请求。

通信基于 JSON 格式，所有请求均为 POST 方法，Content-Type 为 `application/json`。

---

## API 端点

| 端点 | 方法 | 方向 | 说明 |
|------|------|------|------|
| `/health` | GET | 后端 → 扩展 | 健康检查 |
| `/context` | POST | 后端 → 扩展 | 获取 IDE 当前上下文 |
| `/action` | POST | 后端 → 扩展 | 发送操作指令 |

### 健康检查

```
GET /health

Response: 200 OK (无 body)
```

---

## 消息格式定义

### 1. 上下文推送协议 — ContextMessage

后端通过 `/context` 端点请求 IDE 当前上下文。

**请求**：

```json
{}
```

**响应** — ContextMessage 结构：

```json
{
  "active_file": "/path/to/file.py",
  "selected_text": "print('hello')",
  "cursor_position": {
    "line": 10,
    "column": 5
  },
  "open_files": ["/path/to/file.py", "/path/to/other.py"],
  "metadata": {}
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `active_file` | `string \| null` | 是 | 当前活动文件路径 |
| `selected_text` | `string \| null` | 是 | 选中的文本 |
| `cursor_position` | `object \| null` | 是 | 光标位置 `{ line, column }` |
| `open_files` | `string[]` | 是 | 所有打开的文件列表 |
| `metadata` | `object` | 是 | 额外元数据 |

---

### 2. 指令协议 — ActionMessage

后端通过 `/action` 端点向 IDE 发送操作指令。

**请求** — ActionMessage 结构：

```json
{
  "action_type": "open_file",
  "parameters": {
    "file_path": "/path/to/file.py",
    "line": 10,
    "column": 5
  },
  "action_id": "uuid-xxxx"
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `action_type` | `string` | 是 | 操作类型 |
| `parameters` | `object` | 是 | 操作参数 |
| `action_id` | `string` | 是 | 操作唯一标识 |

#### 支持的 action_type

| action_type | 参数 | 说明 |
|-------------|------|------|
| `open_file` | `{ file_path: string, line?: number, column?: number }` | 打开文件并可选跳转 |
| `insert_content` | `{ file_path: string, content: string, position: { line, column } }` | 在指定位置插入内容 |
| `jump_to` | `{ file_path: string, line: number, column?: number }` | 跳转到指定位置 |
| `show_diff` | `{ file_path: string, original_content: string, new_content: string, title?: string }` | 显示差异对比 |
| `get_selection` | `{}` | 获取当前选区和上下文 |

**响应**：

```json
{
  "success": true,
  "data": {}
}
```

或失败时：

```json
{
  "success": false,
  "error": "错误描述"
}
```

---

### 3. 状态更新协议 — StateMessage

连接器状态通过内部状态变量维护，不通过 HTTP 端点暴露。

**StateMessage 结构**：

```json
{
  "state": "connected",
  "detail": "VSCode extension connected"
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `state` | `string` | 是 | 连接器状态 |
| `detail` | `string` | 否 | 状态详情 |

#### 连接器状态枚举

| 状态 | 说明 |
|------|------|
| `disconnected` | 未连接 |
| `connecting` | 正在连接 |
| `connected` | 已连接 |
| `active` | 活跃（正在通信） |
| `disconnecting` | 正在断开 |
| `error` | 错误状态 |

---

## JSON Schema 定义

### ContextMessage Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ContextMessage",
  "type": "object",
  "properties": {
    "active_file": { "type": ["string", "null"] },
    "selected_text": { "type": ["string", "null"] },
    "cursor_position": {
      "oneOf": [
        { "type": "null" },
        {
          "type": "object",
          "properties": {
            "line": { "type": "integer", "minimum": 0 },
            "column": { "type": "integer", "minimum": 0 }
          },
          "required": ["line", "column"]
        }
      ]
    },
    "open_files": {
      "type": "array",
      "items": { "type": "string" }
    },
    "metadata": {
      "type": "object"
    }
  },
  "required": ["active_file", "selected_text", "cursor_position", "open_files", "metadata"]
}
```

### ActionMessage Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ActionMessage",
  "type": "object",
  "properties": {
    "action_type": { "type": "string" },
    "parameters": { "type": "object" },
    "action_id": { "type": "string" }
  },
  "required": ["action_type", "parameters", "action_id"]
}
```

### StateMessage Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "StateMessage",
  "type": "object",
  "properties": {
    "state": {
      "type": "string",
      "enum": ["disconnected", "connecting", "connected", "active", "disconnecting", "error"]
    },
    "detail": { "type": "string" }
  },
  "required": ["state"]
}
```

---

## 连接生命周期

```
1. 扩展启动 → HTTP 服务监听 9741 端口
2. 后端调用 GET /health → 确认扩展可用
3. 后端注册连接器，状态: CONNECTED
4. 后端定期调用 POST /context → 获取上下文
5. 后端调用 POST /action → 发送操作指令
6. 扩展停用 → HTTP 服务关闭
7. 后端检测 /health 失败 → 状态: DISCONNECTED
```
