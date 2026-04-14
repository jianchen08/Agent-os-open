# 工具适配器组件

## 一、需求

### 1.1 组件职责

工具适配器组件负责将外部工具协议转换为统一的工具接口：
- MCP 协议适配
- 文件系统工具适配
- 工具能力映射

### 1.2 对外接口

- `MCPFilesystemAdapter`：MCP 文件系统适配器

### 1.3 依赖

- `tools.registry`：工具注册表
- `tools.executor`：工具执行器
- `core.logging`：日志模块

---

## 二、逻辑

### 2.1 流程设计

#### MCP 适配流程

```
MCP请求 → MCPFilesystemAdapter
              ↓
         协议解析与验证
              ↓
         转换为内部工具调用
              ↓
         执行工具操作
              ↓
         转换结果为MCP响应
```

#### 文件操作流程

```
文件请求 → 适配器
              ↓
         权限验证
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
   读取      写入      删除
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         返回操作结果
```

### 2.2 数据流向

```
外部协议 → Adapter → 内部工具接口
                         ↓
                    ToolExecutor
                         ↓
                    实际执行
                         ↓
Adapter ← 执行结果 ← ToolExecutor
    ↓
协议响应
```

### 2.3 错误处理

- 协议解析失败：返回协议错误响应
- 权限不足：返回权限错误
- 工具执行失败：包装为协议错误返回

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| MCPFilesystemAdapter | MCP 文件系统协议适配 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `mcp_filesystem.py` | MCP 文件系统适配器 |

### 3.3 测试策略

- 单元测试：适配器方法独立测试
- 集成测试：与工具执行器的协作测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 mcp_filesystem.py

```
MCPFilesystemAdapter:
  handle_request(request: MCPRequest) -> MCPResponse: 处理MCP请求
  read_file(path: str) -> bytes: 读取文件
  write_file(path: str, content: bytes) -> bool: 写入文件
  list_directory(path: str) -> List[FileInfo]: 列出目录
  delete_file(path: str) -> bool: 删除文件
```
