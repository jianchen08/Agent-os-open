# ADR 2026-10-06：RP 子系统升级为「AI 驱动交互角色 / 世界框架」

## 背景

之前三轮迭代确立了 AgentOS 角色扮演子系统的核心定位：

- ADR 1：`rp-engine-first-architecture.md` — Engine-first vs LLM-first
- ADR 2：`rp-two-axis-mode-spectrum.md` — 二轴模式谱（LLM 可用性 × 规则复杂度）
- ADR 3：`rp-sillytavern-ecosystem-compatibility.md` — 酒馆增强战略

用户最新反馈指出一个更深层的洞察：

> **"比如这个设计的角色设计是不是可以成为真人一样星露谷游戏。可以是游戏npc的扮演者，这样的话比如游戏道具地图世界观就是该系统的一部分。理解吧。"**

这个洞察揭示：**聊天型 RP 与「真人游戏 NPC / 世界模拟」是同一套架构**。

差别不在原语，只在内容：
- 聊天 RP 的「道具 / 地图 / 世界观」是松散的设定条目
- 游戏 NPC 的「道具 / 地图 / 世界观」是结构化的物品列表、地区、NPC 情感事件

但**它们共享同一套底层原语**（character card / state machine / lorebook / narrative graph / action protocol / memory / LLM）。

如果框架设计能覆盖这两个领域，就能：
- 同时服务"聊天型 RP 创作者"（SillyTavern 用户群）和"游戏 NPC 开发者"（Convai / Inworld 用户群）
- 把 AgentOS 的定位从"RP 插件"提升到"AI 驱动交互角色引擎"

## 决策

**角色扮演子系统正式升级为「AI 驱动交互角色 / 世界框架」（Agent-Driven Interactive Characters & Worlds Framework，简称 ICE / AWF）**。

具体含义：

### 1. 框架定位升级

从"角色扮演插件"升级为"通用 AI 智能体交互框架"。

| 维度 | 旧定位 | 新定位 |
|---|---|---|
| 名称 | RP 插件 | AI 驱动交互角色 / 世界框架 |
| 应用场景 | 仅聊天 RP | 聊天 RP / 游戏 NPC / 模拟经营 / 教育 NPC / 客服 NPC / 陪伴 |
| 用户群 | RP 创作者 | RP + 游戏开发者 + 教育者 + 客服开发者 |
| 价值主张 | "状态机 + LLM" | "同一套原语，适用所有 AI 交互场景" |

### 2. 原语抽象化

七原语的抽象含义：

| 原语 | 抽象 | RP 视角 | NPC 视角 |
|---|---|---|---|
| Character Card | 有名字、有身份、有目标的智能体 | 人设 / 角色 | NPC 定义 |
| State Machine | 智能体的内部可观测状态 | HP/好感/位置 | NPC 的内部经济（库存/需求/好感）|
| World Book | 智能体所在世界的知识条目 | 设定条目 | 物品 / 地区 / 事件 / 食谱 |
| Narrative Graph | 智能体在世界中按时间/事件流转的剧本 | 对话分支 | NPC 日程 + 心路历程（heart events）|
| Action Protocol | 智能体与世界的原子交互 | 表情/特效 | 移动 / 送礼 / 对话 / 工作 |
| Memory | 智能体的历史记忆 | 对话摘要 | NPC 记忆玩家（爱吃的食物 / 生日）|
| LLM | 让智能体能自然语言表达 | 自由对话 | 个性化语气 / 情绪反应 |

### 3. 跨领域应用示例

#### A. 聊天 RP（afengy / SillyTavern 风格）

- Character Card: 1 个角色人设
- State: HP / 好感 / 位置 / 时间
- World Book: 设定条目
- Narrative Graph: 对话分支
- Action: 表情 / 特效
- LLM: 自由对话

#### B. 游戏 NPC（Stardew Valley 风格）

- Character Card: NPC（Abigail）
- State: 好感 / 物品 / 位置 / 日程 / 需求
- World Book: 物品列表 / 地区 / NPC 情感事件 / 食谱
- Narrative Graph: 日程 + 心路历程（heart events）
- Action: 移动 / 送礼 / 对话 / 工作
- LLM: 个性化对话

详见 `docs/working/rp-game-engine-with-llm-enhancement.md` §1.6.2 完整 YAML 示例。

#### C. 模拟城市 NPC

- Character Card: 居民
- State: 饥饿 / 精力 / 工作 / 家庭
- World Book: 资源 / 建筑 / 节日
- Narrative Graph: 居民日程
- Action: 工作 / 交易 / 移动

#### E. 教育 NPC

- Character Card: 老师 / 历史人物
- State: 知识掌握度 / 提问数
- World Book: 教材 / 历史事件
- Narrative Graph: 课程进度
- Action: 讲解 / 提问

#### D. 客服 NPC

- Character Card: 客服机器人
- State: 工单 / 满意度
- World Book: 退款流程 / 商品 FAQ
- Action: 查订单 / 退款 / 转人工（tool calling）

#### F. 心理陪伴

- Character Card: 陪伴 NPC
- State: 情绪日志 / 会话数
- LLM: 自由对话（无模式触发）

### 4. 用户路径（从 RP 角色卡到 NPC 角色卡）

| 阶段 | 工作量 |
|---|---|
| 0. 角色卡只有 prompt（ST 风格）| 0 |
| 1. 加 state.variables（HP / 好感等）| 1-2 小时 |
| 2. 加 lorebook（物品 / 设定）| 1 小时 |
| 3. 加 narrative_graph（日程 / 心路）| 2-3 小时 |
| 4. 加 action 协议（NPC 行为）| 1 小时 |
| 5. 加 LLM 注入规则（人格 / 语气）| 1 小时 |

**总工作量**：半天内把一个聊天 RP 角色变成完整游戏 NPC。

### 5. 架构边界

框架对 AgentOS 内核的接口非常薄：

- 加载角色卡（plugin_loader）
- 调用 LLM（llm_invoker）
- 持久化状态（storage_driver）
- 调度任务（task system）
- 工具注册（tool-surface）

**所有应用领域** 都用同一个内核接口。差别在角色卡内容，不在内核。

### 6. 与友商产品的对比

| 平台 | 定位 | 与本框架对比 |
|---|---|---|
| **Convai** | 游戏 NPC AI（Unity/Unreal 集成）| 同愿景；本框架对工具与生态更广（AgentOS 内核 + ST 兼容 + 多领域）|
| **Inworld AI** | 同 Convai（已收缩到 TTS/STT）| 同上 |
| **Hidden Door** | 故事世界 | 同愿景；本框架有 ST 兼容优势 |
| **SillyTavern / RisuAI** | 聊天 RP | 本框架 = ST 增强（兼容 + 加游戏 NPC 能力）|
| **afengy.com** | 中文成人 RP 聊天 | 本框架 = 增强（state machine + RAG）|
| **character.ai** | 简单聊天 | 本框架太重，但 ST 资产可走 character.ai 风格 |

**核心定位 = "Convai 之父 + SillyTavern 之母"**——结合游戏 NPC 的状态机严谨 + ST 的 UGC 生态。

## Alternatives Considered

### A. 保持"仅 RP"定位

- 优点：聚焦
- 缺点：错过游戏 NPC 巨大市场（Convai / Inworld / Hidden Door / Stardew Valley mod 玩家）
- 否决理由：用户明确指出这是同一套架构

### B. 把 RP 子系统和 NPC 子系统分开

- 优点：边界清晰
- 缺点：重复发明原语，资源浪费
- 否决理由：**两者原语 95% 相同**，强行分开得不偿失

### C. 改名（脱离 RP 子系统）

- 优点：减少 RP 包袱
- 缺点：与用户预期不符（AgentOS 0.2 已定义 RP 子系统）
- 缓解：在文档中明确"RP 是本框架的最大应用之一"，但框架本身是通用的

### D. 升级为通用框架（采纳）

- 优点：覆盖更多应用；RP / NPC / 教育 / 客服 / 陪伴共享代码
- 缺点：术语抽象、文档复杂
- 缓解：在文档中明确"RP 是入口，应用领域是扩展"

## 影响

### 正面

- **应用领域扩张** —— 从 RP 单一领域扩展到 6+ 个领域
- **用户群扩张** —— 从 ST 用户扩展到游戏开发者 + 教育者 + 客服开发者
- **代码复用** —— 同一套原语支持多种应用，避免重复发明
- **竞争优势** —— 比 Convai / Inworld 更通用；比 ST 更结构化

### 负面

- **文档抽象度提升** —— 普通 RP 创作者可能感到困惑
- **抽象泄漏风险** —— 某些原语（如 narrative_graph）在游戏 NPC 中与聊天 RP 中用法差异大
- **学习曲线** —— 多领域用户的学习起点不同

### 中性

- 既有 RP 子系统代码不变（因为原语兼容）
- 既有 plugin_loader / llm_invoker / storage_driver 接口不变

## 依赖与解锁

依赖：
- ADR 1/2/3（已采纳）
- M0（ST 兼容，正在 M0 中）

解锁：
- 插件命名扩展（`agentos.rp.*` → `agentos.character.*` 或保留 `agentos.rp.*` 作为兼容层）
- §1.6 应用领域扩展章节
- 案例库（多领域示例）

## 关键洞察（用户原话）

> "比如这个设计的角色设计是不是可以成为真人一样星露谷游戏。"
> "可以是游戏npc的扮演者，这样的话比如游戏道具地图世界观就是该系统的一部分。"
> "理解吧。"

**这揭示了框架的本质：不是 RP 工具，而是 AI 交互角色 / 世界引擎。RP 是它的最大应用，但不是全部。**

## 归档

- 决策日期：2026-10-06
- 主要产出文档：
  - `docs/working/rp-game-engine-with-llm-enhancement.md`（§1.6 新增章节）
  - 本 ADR
- 状态：**已采纳**
- 后续行动：在 M0-M3 实施过程中，明确该框架的通用性；M3+ 增加游戏 NPC / 教育 / 客服示例