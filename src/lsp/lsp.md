# LSP 模块

## 需求

### 职责
提供 LSP (Language Server Protocol) 网关服务，支持代码理解能力，包括跳转到定义、查找引用、代码诊断和代码补全。同时提供 IDE 检测和文件跳转功能。

### 对外接口
- 输入：文件路径、位置信息、语言类型
- 输出：定义位置、引用列表、诊断信息、补全项

### 依赖
- 依赖模块：无内部依赖
- 外部依赖：psutil

## 逻辑

### 流程设计
```
LSP 请求 → IDE 检测 → 获取/创建客户端 → 执行 LSP 操作 → 返回结果
                ↓
    ┌───────────┼───────────┐
    ↓           ↓           ↓
 VSCode     JetBrains     Nvim
```

### 数据流向
1. IDE 检测：进程/文件/环境变量 → IDE 信息
2. LSP 操作：文件 URI + 位置 → LSP 服务器 → 结果
3. 文件跳转：URI → 解析 → IDE 命令 → 打开文件

### 数据模型
#### IDE 类型
| 类型 | 说明 |
|------|------|
| VSCODE | Visual Studio Code |
| JETBRAINS | JetBrains IDE (IntelliJ, PyCharm 等) |
| NVIM | Neovim |
| EMACS | Emacs |
| VS | Visual Studio |
| UNKNOWN | 未知 IDE |

#### LSP 服务器信息
| 字段 | 类型 | 说明 |
|------|------|------|
| name | str | 服务器名称 |
| version | str | None | 服务器版本 |
| language | str | 支持的语言 |
| command | str | 启动命令 |
| args | list[str] | 启动参数 |
| env | dict | None | 环境变量 |

#### 位置信息
| 字段 | 类型 | 说明 |
|------|------|------|
| line | int | 行号（从0开始） |
| character | int | 字符偏移（从0开始） |

#### 定义/引用位置
| 字段 | 类型 | 说明 |
|------|------|------|
| uri | str | 文档 URI |
| range | Range | 范围 |

#### 诊断信息
| 字段 | 类型 | 说明 |
|------|------|------|
| range | Range | 诊断范围 |
| severity | int | 严重程度：1=Error, 2=Warning, 3=Info, 4=Hint |
| code | str | None | 诊断代码 |
| source | str | None | 诊断源 |
| message | str | 诊断消息 |

#### 补全项
| 字段 | 类型 | 说明 |
|------|------|------|
| label | str | 补全项显示文本 |
| kind | int | None | 补全项类型 |
| detail | str | None | 补全项详情 |
| documentation | str | None | 补全项文档 |
| sortText | str | None | 排序文本 |
| insertText | str | None | 插入文本 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `LSPGateway` | LSP 网关类，管理多个语言的 LSP 客户端 |
| `LSPClient` | LSP 客户端类，连接到 LSP 服务器 |
| `IDEDetector` | IDE 检测器类，自动检测当前使用的 IDE |
| `FileJumpProtocol` | 文件跳转协议类，支持在不同 IDE 中打开文件 |
| `get_lsp_gateway() -> LSPGateway` | 获取全局 LSP 网关实例 |

#### LSPGateway API
| 接口 | 职责 |
|------|------|
| `LSPGateway.initialize() -> None` | 初始化 LSP 网关 |
| `LSPGateway.shutdown() -> None` | 关闭 LSP 网关 |
| `LSPGateway.go_to_definition(file_path: str, position: Position, language: str | None) -> list[Location]` | 跳转到定义 |
| `LSPGateway.find_references(file_path: str, position: Position, language: str | None) -> list[Location]` | 查找引用 |
| `LSPGateway.get_diagnostics(file_path: str, language: str | None) -> list[Diagnostic]` | 获取诊断信息 |
| `LSPGateway.get_completion(file_path: str, position: Position, language: str | None) -> list[CompletionItem]` | 获取代码补全 |
| `LSPGateway.get_client(language: str) -> LSPClient | None` | 获取指定语言的 LSP 客户端 |
| `LSPGateway.get_supported_languages() -> list[str]` | 获取支持的语言列表 |
| `LSPGateway.get_ide_info() -> IDEInfo | None` | 获取 IDE 信息 |

#### LSPClient API
| 接口 | 职责 |
|------|------|
| `LSPClient.start() -> bool` | 启动 LSP 服务器 |
| `LSPClient.stop() -> None` | 停止 LSP 服务器 |
| `LSPClient.go_to_definition(uri: str, position: Position) -> list[Location]` | 跳转到定义 |
| `LSPClient.find_references(uri: str, position: Position, context: dict | None) -> list[Location]` | 查找引用 |
| `LSPClient.get_diagnostics(uri: str) -> list[Diagnostic]` | 获取诊断信息 |
| `LSPClient.get_completion(uri: str, position: Position, context: dict | None) -> list[CompletionItem]` | 获取代码补全 |
| `LSPClient.open_document(uri: str, language_id: str, version: int, text: str) -> None` | 打开文档 |
| `LSPClient.change_document(uri: str, version: int, changes: list[dict]) -> None` | 修改文档 |

#### IDEDetector API
| 接口 | 职责 |
|------|------|
| `IDEDetector.detect() -> IDEInfo | None` | 检测当前运行的 IDE |
| `IDEDetector.detect_all() -> list[IDEInfo]` | 检测所有运行的 IDE |
| `IDEDetector.get_ide_type(name: str) -> IDEType` | 根据 IDE 名称获取类型 |

#### FileJumpProtocol API
| 接口 | 职责 |
|------|------|
| `FileJumpProtocol.jump_to_file(file_path: str, position: Position | None, ide_info: IDEInfo | None) -> bool` | 跳转到文件指定位置 |
| `FileJumpProtocol.parse_uri(uri: str) -> tuple[str, Position | None]` | 解析文件 URI |
| `FileJumpProtocol.jump_from_uri(uri: str) -> bool` | 从 URI 跳转到文件 |
| `FileJumpProtocol.generate_uri(file_path: str, position: Position | None, ide_type: IDEType | None) -> str` | 生成文件 URI |

### 配置设计
#### 支持的语言
| 语言 | LSP 服务器 |
|------|------------|
| python | pylsp |
| javascript | typescript-language-server |
| typescript | typescript-language-server |
| go | gopls |
| rust | rust-analyzer |

### 错误处理
- LSP 服务器启动失败：记录日志，跳过该语言
- LSP 操作失败：返回空列表或抛出异常
- IDE 检测失败：返回 None，使用默认方式打开文件

### 安全设计
- 文件跳转使用 shell=False 避免命令注入
- 文件存在性检查

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `LSPGateway`：LSP 网关类
- `LSPClient`：LSP 客户端类
- `IDEDetector`：IDE 检测器类

#### client.py
职责：LSP 客户端
暴露接口：
- `LSPClient`：LSP 客户端类
- `LSPClient.__init__(server_info: LSPServerInfo)`：初始化客户端
- `LSPClient.start() -> bool`：启动 LSP 服务器
- `LSPClient.stop() -> None`：停止 LSP 服务器
- `LSPClient.go_to_definition(uri: str, position: Position) -> list[Location]`：跳转到定义
- `LSPClient.find_references(uri: str, position: Position, context: dict | None) -> list[Location]`：查找引用
- `LSPClient.get_diagnostics(uri: str) -> list[Diagnostic]`：获取诊断信息
- `LSPClient.get_completion(uri: str, position: Position, context: dict | None) -> list[CompletionItem]`：获取代码补全
- `LSPClient.open_document(uri: str, language_id: str, version: int, text: str) -> None`：打开文档
- `LSPClient.change_document(uri: str, version: int, changes: list[dict]) -> None`：修改文档

#### detector.py
职责：IDE 检测器
暴露接口：
- `IDEDetector`：IDE 检测器类
- `IDEDetector.detect() -> IDEInfo | None`：检测当前运行的 IDE
- `IDEDetector.detect_all() -> list[IDEInfo]`：检测所有运行的 IDE
- `IDEDetector.get_ide_type(name: str) -> IDEType`：根据 IDE 名称获取类型

#### file_jump.py
职责：文件跳转协议
暴露接口：
- `FileJumpProtocol`：文件跳转协议类
- `FileJumpProtocol.jump_to_file(file_path: str, position: Position | None, ide_info: IDEInfo | None) -> bool`：跳转到文件
- `FileJumpProtocol.parse_uri(uri: str) -> tuple[str, Position | None]`：解析文件 URI
- `FileJumpProtocol.jump_from_uri(uri: str) -> bool`：从 URI 跳转
- `FileJumpProtocol.generate_uri(file_path: str, position: Position | None, ide_type: IDEType | None) -> str`：生成文件 URI

#### gateway.py
职责：LSP 网关服务
暴露接口：
- `LSPGateway`：LSP 网关类
- `LSPGateway.initialize() -> None`：初始化网关
- `LSPGateway.shutdown() -> None`：关闭网关
- `LSPGateway.go_to_definition(file_path: str, position: Position, language: str | None) -> list[Location]`：跳转到定义
- `LSPGateway.find_references(file_path: str, position: Position, language: str | None) -> list[Location]`：查找引用
- `LSPGateway.get_diagnostics(file_path: str, language: str | None) -> list[Diagnostic]`：获取诊断
- `LSPGateway.get_completion(file_path: str, position: Position, language: str | None) -> list[CompletionItem]`：获取补全
- `LSPGateway.get_client(language: str) -> LSPClient | None`：获取客户端
- `LSPGateway.get_supported_languages() -> list[str]`：获取支持的语言
- `LSPGateway.get_ide_info() -> IDEInfo | None`：获取 IDE 信息
- `get_lsp_gateway() -> LSPGateway`：获取全局网关实例

#### types.py
职责：LSP 类型定义
暴露接口：
- `IDEType`：IDE 类型枚举
- `Position`：文档位置模型
- `Range`：文档范围模型
- `Location`：定义/引用位置模型
- `Diagnostic`：诊断信息模型
- `CompletionItem`：补全项模型
- `LSPRequest`：LSP 请求模型
- `LSPResponse`：LSP 响应模型
- `LSPServerInfo`：LSP 服务器信息模型
- `IDEInfo`：IDE 信息模型
- `LSPErrorCode`：LSP 错误码枚举

### 测试策略
#### 模块测试
- 单元测试：IDE 检测、URI 解析、文件跳转
- 集成测试：LSP 客户端连接、LSP 操作
- Mock 策略：Mock LSP 服务器响应

## 实现
→ 见代码文件
