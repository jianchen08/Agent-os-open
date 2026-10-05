# ADR 2026-10-06（修订）：角色扮演子系统采用「二轴模式谱」架构

> **本 ADR 是 `2026-10-06-rp-engine-first-architecture.md` 的修订版。**
> 原 ADR 提出「Engine-first」三模式枚举；用户评审指出两个问题：
> 1. 不应该强制推 Engine-first，应当支持三种模式（纯 LLM / 引擎+LLM / 纯引擎）都作为 first-class
> 2. 输出契约不应过死，应当可调
>
> 同时强调：「无 LLM」不应做成完整 CRPG，而是**小型流程化游戏**（视觉小说级）；规则复杂度是连续可调而非开关。

## 背景

AgentOS 角色扮演子系统设计在迭代中沉淀出以下共识：

1. 「Engine-first vs LLM-first」的对立是错的——应当都支持
2. 规则复杂度是**连续变量**而非开关：0 规则 = afengy 等价物；少规则 = 桥接模式；多规则 = 完整引擎
3. 「无 LLM」= 视觉小说 / 文字冒险，**不是完整 CRPG**（不追求复杂 RPG 体验，只追求"游戏还能玩"）
4. 常规状态（HP/位置/时间）**永远由代码持有**；模糊状态（好感/剧情冲突）可由 LLM 自由发挥
5. 输出契约松紧可调：从纯文本到全结构化（4 档）
6. 记忆复用 AgentOS 内核 session/storage，不重新发明

## 决策

**采用「二轴模式谱」架构**：

| 轴 | 取值 | 含义 |
|---|---|---|
| **轴 1：LLM 可用性** | `with_llm` / `without_llm` | 是否调用 LLM 生成叙事 |
| **轴 2：规则复杂度** | `none` / `core` / `full` | 创作者声明多少规则 |

四个 cell 都是 first-class：

| | with_llm | without_llm |
|---|---|---|
| **none** | afengy 等价物（LLM 全权） | 无意义（跳过） |
| **core**（★默认）| AgentOS 默认模式：常规状态=代码，模糊状态=LLM | 离线小型流程游戏（visual novel） |
| **full** | Convai 风格复杂 RP | CRPG-lite |

### 关键设计原则

1. **常规状态永远由代码持有** —— 即使规则复杂度为 `none`，只要声明了 HP/MP/位置/时间，代码就持有，LLM 不能改
2. **模糊状态可由 LLM 自由发挥** —— 好感度、剧情冲突、NPC 情绪细节：LLM 在 `core` 模式下可自由输出，代码不校验
3. **规则是连续变量** —— `core` 与 `full` 之间无硬边界；创作者可逐步声明更多规则
4. **契约松紧可调** —— 输出格式不是单一 JSON Schema，而是 `contract_level` 4 档
5. **记忆复用 AgentOS 内核** —— 不重新发明 session/storage
6. **无 LLM = 视觉小说级** —— 不追求 CRPG 复杂度，只追求"游戏还能玩"

### 默认推荐 cell

**`core + with_llm`** —— 80% 创作者用这个：
- LLM 写故事
- 常规状态（HP/位置/时间/物品/基础战斗）由代码持有
- 模糊状态（好感/剧情冲突）由 LLM 自由发挥
- LLM 挂时自动切到 `core + without_llm`（如果角色卡有 narrative_graph）

## Alternatives Considered

### A. 三模式分类（A / B / C）

- 优点：清晰
- 缺点：**离散枚举式+容易让用户误以为必须选一个**，且无法表达「声明一点规则但不要全部」的中间态
- 否决理由：用户反馈要求支持连续可调

### B. 单一「Engine-first」路线

- 优点：清晰
- 缺点：**强制要求所有角色卡都有状态机**，对只想做轻量 RP 的创作者门槛过高；与市面 afengy/character.ai 等主流生态不兼容
- 否决理由：用户明确指出 afengy 模式是合法 first-class

### C. 单一「LLM-first」路线

- 优点：开发快
- 缺点：状态漂移、合规风险（依赖 jailbreak）、token 浪费、无重玩性
- 否决理由：与 AgentOS 0.2 既有能力（agent_config_load、状态机、规则引擎）契合度差

### D. 二轴 + 默认推荐 cell = `core + with_llm`（采纳）

- 优点：
  - 支持所有合法 cell，无需用户做模式选择
  - 默认 cell 满足 80% 创作者（HP/MP/位置/时间 + 一段 prompt）
  - 「无 LLM」是视觉小说级（够小才真实）
  - 与 afengy 兼容（`none + with_llm` 等价）
  - 与 Convai 兼容（`full + with_llm`）
  - 记忆复用现有内核
- 缺点：
  - 「无 LLM」需要 narrative_graph + 模板，门槛比纯 LLM 聊天高一点
  - 玩家理解二轴比单选模式稍复杂
- 缓解：
  - 默认值合理（`core + with_llm`），大多数用户不用关心二轴
  - UI 显示「离线模式 / 自由聊天模式 / 严格规则」三档按钮，对应二轴直觉

## 影响

### 正面

- **架构清晰**：二轴独立滑点，不互相耦合
- **包容现有生态**：afengy 模式（`none+with_llm`）和 Convai 模式（`full+with_llm`）都是合法 cell
- **降级自然**：LLM 健康度低自动从 `with_llm` 切到 `without_llm`，无需复杂降级逻辑
- **离线可用**：玩家主动切到 `without_llm`，或 LLM 挂了自动切
- **创造者门槛分层**：从零规则（afengy 等价）到全规则（Convai 等价）连续可调

### 负面

- **「无 LLM」需要 narrative_graph**：不能用一个简单的 prompt 文件切换
- **二轴 UI**：玩家需要理解「规则复杂度」轴（虽然默认 `core`）

### 中性

- 既有 agent_config_load 路线仍可作为「纯 LLM 聊天模式」存在，不冲突
- DSH 适配器、系统插件与本设计正交

## 依赖与解锁

无依赖。

解锁以下 ADR：
- 角色卡 spec 选型（YAML vs JSON）
- 状态机契约格式（JSON Schema vs Protobuf）
- 常规 vs 模糊状态边界
- 世界书触发策略
- Action 协议扩展点
- 持久化策略
- 记忆复用策略（对接 AgentOS 内核 session/storage）
- CustomCSS 沙箱
- 角色卡导出格式
- LLM 健康度检测
- 叙事图 schema
- 状态变更审计

## 与上一版 ADR 的关系

- **上一版**（`2026-10-06-rp-engine-first-architecture.md`）作为「历史快照」保留，不删除
- **本版**（本 ADR）覆盖上一版的"三模式枚举"立场
- 设计文档 `docs/working/rp-game-engine-with-llm-enhancement.md` 已修订至本 ADR 立场

## 归档

- 决策日期：2026-10-06
- 调研输入：afengy.com（含创作指南）、SillyTavern、RisuAI、Convai、Inworld、Hidden Door、NovelAI、character.ai、JanitorAI、SpicyChat、筑梦岛等
- 用户反馈点：
  - 「首先要能退化成普通的游戏或者llm扮演」
  - 「输出契约不能太死」
  - 「无 LLM 应是小型流程化游戏，不是 CRPG」
  - 「记忆应考虑使用现有的记忆」
- 主要产出文档：
  - `docs/working/rp-game-engine-with-llm-enhancement.md`（详细设计）
  - `docs/working/afengy-creator-guide.md`（创作指南原文）
  - `docs/working/afengy-creator-guide-raw.html`（原始 HTML）
- 状态：**已采纳**，进入实施路线 Phase 1（M1）

## 实施优先

| 阶段 | cell | 用户群 | 优先级 |
|---|---|---|---|
| M1 | `core + with_llm` | 80% 创作者 | ★★★★★ |
| M2 | `core + without_llm` | 离线 / 节省 / 视觉小说 | ★★★★ |
| M3 | `full + with_llm` | 复杂 RP | ★★★ |
| M4 | `full + without_llm` + 生态迁移 | 高级 + 老用户 | ★★ |