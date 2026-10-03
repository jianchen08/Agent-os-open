# 2026-10-03 思考选择去映射化：选项即参数组，业务知识全面后置

## 背景

聊天输入框的思考模式选择器原为 llm_core `ui_schema` 声明的静态四档
（off/low/medium/high），配套一套档位词汇映射链：

- 前端：档位枚举类型、`STRENGTH_TO_ENABLE`（off→enableThinking=false）、
  温度/effort 反推档位的启发式（`mapParamsToStrength`）、`switchThinkingMode`
  参谋调用；
- 后端：`llm.yaml models.<id>.thinking_strength_params`（档位键 → 参数组）、
  llm_core `resolve_thinking_strength_params` 档位查表（厂商级 > 模型级）、
  `_config_models` 桥接把两级映射注入 llm_core。

问题：静态四档与"模型/厂商配置了什么参数"脱节（每模型思考参数形态不同：
DeepSeek reasoning_effort / MiniMax adaptive thinking / 无推理普通模型），
且档位词汇层（off/low/medium/high）在前后端各留了一份业务知识。

用户裁定（2026-10-03，连续三轮收敛）：

1. 选项 = 模型或厂商配置的参数本身，**不要档位→参数映射层**；
2. 输入框选项按钮应全部来自后端推送数据，宿主组件不承载思考业务知识
   （只留最基本的发送），业务知识（派生、文案、当前值）只在后端。

## 决策

**选项即参数组，消息携带参数组，执行端零查表。**

1. **选项数据面**（llm_service 新端点 `GET /ext/llm_service/config/llm/
   thinking-levels?model=<name>`，`get_thinking_levels`）：
   - 选项 = `thinking_strength_params` 配置的**参数组本身**（厂商级在前、
     模型级补位，按参数内容去重，配置顺序即选项顺序）；
   - `label` = 参数渲染（`reasoning_effort=max`、`thinking={"type":"disabled"}`），
     无档位中文文案表；
   - `value` = 参数组紧凑 JSON 串（sort_keys）——即消息线上形态；
   - `current` = 模型 `default_params` 思考参数（reasoning_effort 精确相等
     优先，其次 thinking.type）命中的选项 value，未匹配 null——反向推断
     真值源在后端，前端零推断。
2. **消息契约**：`user_input.thinking_strength` 携带参数组 JSON 串
   （'' = 不覆盖）。内核照旧不透明透传（TEXT 列，零改动）。
   `enable_thinking` 字段前端停发（内核无消费方，协议字段保留恒 false）。
3. **执行面**（llm_core）：`resolve_thinking_strength_params` 档位查表删除，
   改 `resolve_thinking_params`：JSON 解析 → `_THINKING_STRENGTH_ALLOWED`
   白名单过滤（仅 reasoning_effort/thinking，采样参数不随消息覆盖）→
   与 default_params 合并。模型级/厂商级映射配置读取与 `_config_models`
   桥接（`provider_thinking_strength_params`）一并删除——llm_core 不再读
   `thinking_strength_params`。
4. **前端**：宿主组件只做「拉取选项 → 声明渲染 → 选中值随消息透传」：
   - `useThinkingLevelsQuery`（选项/当前值，模型名变化自动重拉）；
   - ChatContainer 显示值 = 标签显式记忆（∈ 选项时生效）?? 端点 current
     ?? ''；选择器随选项空隐藏；
   - ChatInput 经受控桥把选项整体注入 llm_core 声明的 select（replace
     模式，`task_mode` append 模式同函数参数化 `selectFieldsWith`）；
   - 标签记忆（thinkingModeStore）只存字符串，未选择 = ''；默认档
     `DEFAULT_THINKING_STRENGTH`/`STRENGTH_TO_ENABLE` 删除；
   - llm_core `plugin.json` 声明去静态 options（选项由宿主注入）。
5. **保留不动**：`llm.yaml thinking_strength_params` 配置形态与设置页
   ModelParamsEditor 编辑面（levels 行 × 参数组）——配置契约未变，变的
   只是消费方（llm_core 查表 → thinking-levels 端点直读）；llm_presets
   `thinking_strength.allowed_keys` 与 llm_core 白名单的对账闸保留。

## Alternatives Considered

1. **前端按 llmConfig 派生选项（第一版实现）**：厂商/模型优先级、中文
   文案表放前端 utils——被否：业务知识泄漏到宿主，llmConfig 与选项两份
   数据源，违反"选项全部后端推送"裁定。
2. **后端派生选项但保留档位键语义（第二版实现）**：选项带档位键 value、
   中文 label，llm_core 仍按键查表——被否：用户明确"不需要映射"，档位
   词汇层是多余抽象；关闭档（off）特判与 enableThinking 布尔随之成为
   死知识。
3. **改内核消息契约**（新 thinking_params 结构化字段替代字符串）：
   否——内核对该字段是不透明透传，JSON 串即达语义，动内核契约（冻结期）
   无收益。
4. **删除 thinking_strength_params 配置、选项直接来自 default_params**：
   否——default_params 每参数只有一个值，提供不了可切换的多档；配置的
   参数组集合就是"可切换面"，设置页编辑面也依赖该键。

## 影响

- 前端：`utils/thinkingStrength.ts` 删除（mapParamsToStrength/
  findModelParams 死代码）；`services/api/thinkingMode.ts` 及 THINKING_MODE
  常量删除（switchThinkingMode 参谋调用随映射语义消亡）；router/
  GlobalWebSocket 的 enableThinking 链删除。
- 后端：llm_service 新端点 + 生成物投影（endpoints.generated.ts 再生成）；
  llm_core 思考链路净删约 40 行；`routes_thinking_mode.py` 旧 thinking-mode
  域（models/info/check/switch/recommendations）已无前端消费方，本轮不删
  （独立退役决策另行处理）。
- 兼容：内部一刀切——旧档位词汇（裸键名串）不再被 llm_core 识别（非
  JSON → 忽略不覆盖，不抛错）；localStorage 旧档位值（'high' 等）因不
  在新选项集内自动回落端点 current，无脏状态。
- e2e：`scripts/e2e_m1_trigger.py` 发 `thinking_strength: ""`，语义不变。

## 归档

- 实现：llm_service `routes_llm_config.get_thinking_levels` +
  `server.py` 分发 + `plugin.json` 端点声明；llm_core `plugin.py`
  `resolve_thinking_params`；前端 `useLlmQueries.useThinkingLevelsQuery` +
  ChatContainer/ChatInput 透传。
- 测试：`test_routes_llm_config_thinking_levels.py`（选项去重/标签渲染/
  current 匹配/空面）、`test_llm_core_thinking_params.py`（白名单过滤/
  非法输入）、`test_config_models_bridge.py`（不桥接断言）、前端
  ChatContainerGaps/ChatInput.layout/thinkingModeStore。
