# services

## 需求
### 职责
提供 LLM 相关的高级服务，包括思考模式切换、模型推荐等。

### 对外接口
- 输入：模型名称、消息列表、配置参数 → 输出：LLM 响应或配置信息

### 依赖
- 依赖组件：llm/clients（LLM 客户端工厂）、llm/config（思考模型配置）
- 外部依赖：无

## 逻辑
### 流程设计
1. 接收模型名称和配置参数
2. 判断是否支持思考模式
3. 根据思考模式类型（参数切换/模型切换）选择策略
4. 获取对应客户端并生成响应
5. 后处理响应（提取思考内容）

### 数据流向
```
模型名称 → 思考模式配置 → 客户端选择 → LLM 调用 → 响应处理
```

### 配置设计
#### 思考模式类型
| 类型 | 说明 | 示例模型 |
|------|------|----------|
| parameter_switch | 通过参数启用思考模式 | GLM-4.7 |
| model_switch | 切换到专门的思考模型 | DeepSeek R1 |

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出服务类
暴露接口：
- `ThinkingModeService`：思考模式服务
- `get_thinking_mode_service() -> ThinkingModeService`：获取服务实例

#### thinking_mode.py
职责：思考模式切换服务
暴露接口：
- `ThinkingModeService`：思考模式服务类
  - `get_available_thinking_models() -> list[dict]`：获取支持思考模式的模型列表
  - `can_enable_thinking_mode(model_name: str) -> bool`：检查是否支持思考模式
  - `get_thinking_mode_info(model_name: str) -> dict | None`：获取思考模式信息
  - `generate_with_thinking_mode(model_name: str, messages: list[Message], enable_thinking: bool, **kwargs) -> LLMResponse`：带思考模式生成
  - `get_thinking_mode_params(model_name: str, enable_thinking: bool) -> dict`：获取思考模式参数
  - `switch_thinking_mode(current_model: str, enable_thinking: bool) -> tuple[str, dict]`：切换思考模式
  - `get_thinking_mode_recommendations(task_type: str, complexity: str) -> list[dict]`：获取推荐

- `get_thinking_mode_service() -> ThinkingModeService`：获取服务实例

### 测试策略
#### 组件测试
- 单元测试：思考模式判断、参数生成
- 集成测试：与 LLM 客户端的集成

## 实现
→ 见代码文件
