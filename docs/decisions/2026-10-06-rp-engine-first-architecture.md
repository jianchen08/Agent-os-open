# ADR 2026-10-06：角色扮演模式采用「Engine-first」架构（游戏 RP 为基，LLM 为增强层）

## 背景

AgentOS 0.2 现状（详见 `docs/working/rp-game-engine-with-llm-enhancement.md`）：

- 内核（Rust）+ Python 插件 + React 前端
- 架构公理：「一切皆插件」，内核仅做执行基座
- 已有的 RP 路线：从内核 `executor/general_agent.yaml` 装载 agent 配置 → 通过 `tool-surface` 注入 LLM 可见工具面
- 现有能力：LLM 工具调用（声明即注册）、流式推理、记忆宫殿（memory_palace）、正则后处理（regex_replaces）

市面上两种主流路线：

1. **LLM-first**（afengy.com、character.ai、JanitorAI、SpicyChat、筑梦岛、NovelAI 等）
   - LLM 是核心，状态、规则、世界一致性靠 prompt + Mod 缝补
   - 优势：开发快、试错成本低、容易启动
   - 劣势：状态漂移、token 浪费、无重玩性、合规风险（依赖 jailbreak）
2. **Engine-first**（Convai、Inworld AI、Hidden Door、Charisma.ai 部分）
   - 游戏引擎为核心，LLM 作为叙事生成器
   - 优势：确定性、可重玩、可视化、合规、可降级
   - 劣势：开发成本高、插件多、需要创作者学习引擎

需要在 AgentOS 中确定 RP 模式的架构立场。

## 决策

**采用 Engine-first 架构**：把状态机、规则引擎、叙事图、Action 协议、记忆系统等作为一等公民设计，LLM 仅作为「故事生成器」嵌入引擎的一个模块。

具体地：

1. **状态机子系统**（`agentos.rp.state_machine`）
   - 状态由 SQLite 持久化，per-task_id
   - 角色卡 `engine.variables` 强 schema 声明（类型、范围、tag）
   - LLM 输出必须经过 `proposed_state_changes` → 契约校验 → 落库
   - LLM 不能直接修改状态，只可以「提议」
2. **叙事图子系统**（`agentos.rp.narrative_graph`）
   - 节点 + 决策 + 触发器结构
   - 兼容 Convai 的 `<speak>` 与 `*` 语法（v1 兼容层）
   - 创作者可视化编辑器
3. **规则引擎**（`agentos.rp.combat` / `agentos.rp.rules`）
   - 战斗公式、随机事件、掉落、好感判定由确定性代码计算
   - 数值由 `engine.rules` 配置化，运营可调不改
   - 随机使用 `Random(seed)`，结果可复现
4. **Action 协议**
   - LLM 输出 `suggested_actions`，由代码 `ActionExecutor` 执行
   - 每个 action 一个 handler，失败不抛（一个 action 失败不影响整个回合）
5. **降级模式**
   - `agentos.rp.llm.template_narrator`：模板叙事器
   - LLM 不可用 / 超时 / 限流时自动降级
   - 游戏在无 LLM 时仍可玩
6. **记忆系统**（三层）
   - L0 短期：最近 N 轮直接进 prompt
   - L1 中期：每 5-10 轮由 LLM 二次摘要
   - L2 长期：跨会话的玩家档案 + 向量检索
7. **角色卡标准**（AgentOS V1）
   - YAML / JSON 双格式
   - 支持导入 SillyTavern V2/V3 PNG、导出 PNG-V3 与 JSON
   - `engine.variables` / `engine.actions` / `engine.narrative_graph` / `engine.rules` / `engine.lorebook` / `engine.ui` 六大模块**

## Alternatives Considered

### A. LLM-first 路线

- 优点：开发快、与现有 AgentOS 路线（基于 general_agent + tool-surface）契合度高
- 缺点：状态漂移、合规风险（依赖 jailbreak）、token 浪费、无重玩性
- 否决理由：违背「一切皆插件」公理——内核会变成「LLM 编排器」而非「插件装载器」；市面已有 afengy.com 等成熟玩家，再做一个 LLM-only 没有差异化

### B. 只做「结构化输出 + 契约校验」，不做真正的游戏引擎

- 优点：开发量最小；保留 LLM-first 路线的灵活性
- 缺点：没有真正的状态机、规则引擎、叙事图，仍是 LLM 增强 + 提示词工程的拼凑
- 否决理由：用户调研显示，Convai / Inworld 等「真游戏引擎 + LLM 增强」产品才是 RP 平台的终局形态；半路路线无法建立护城河

### C. 完全剥离 LLM，纯游戏引擎（不集成 LLM）

- 优点：架构最干净、规则最确定
- 缺点：失去 LLM 在叙事 / 自然语言 / 个性化回应上的优势
- 否决理由：与 AgentOS「LLM 是核心能力」的定位矛盾；失去与市面 RP 平台的竞争优势

### D. Engine-first，但每个插件各自实现 LLM 调用（不集中）

- 优点：插件解耦
- 缺点：LLM 调用散落各处，难以统一管理 prompt 拼装、降级、token 计量
- 否决理由：违反「契约冻结」公理——所有 LLM 输出契约应统一；不便于降级模式

### E. Engine-first，且中央化一个 `agentos.rp.llm.narrator` 服务（采纳）

- 优点：契约统一、降级模式简单、token 计量清晰、prompt 拼装逻辑单一
- 缺点：成为依赖中心，需要严格文档化
- 缓解：契约由 JSON Schema 描述，自动生成 SDK；提供 mock 服务便于离线测试

## 影响

### 正面

- **架构清晰**：状态、规则、叙事、LLM 各司其职，无职责重叠
- **可测试**：状态机和规则引擎可纯函数测试；LLM 调用有契约校验
- **可降级**：LLM 不可用时仍可玩，对稳定性是巨大优势
- **可扩展**：新插件可复用状态机 / 叙事图 / Action 协议；创作者可在不碰内核的前提下扩展玩法
- **差异化**：相比 afengy.com / 筑梦岛等 LLM-only 玩家，建立「游戏 RP 引擎」技术壁垒

### 负面

- **开发成本高**：需实现 ~12 个插件（M1-M4 共 12 周）
- **创作者学习曲线**：从「写 prompt」变为「配置游戏规则包」
- **LLM 调用模板是规范**，破坏需 ADR + 兼容机制

### 中性

- **既有 agent_config_load 路线**：仍可作为「纯 LLM 聊天模式」存在，不冲突
- **DSH 适配器**（系统插件）：与本设计正交

## 依赖与解锁

- 不依赖其他未决 ADR
- 解锁以下 ADR（详见设计文档 §14）：
  - 角色卡 spec 选型（YAML vs JSON）
  - 状态机契约格式（JSON Schema vs Protobuf）
  - 世界书触发策略（关键词 vs RAG）
  - Action 协议扩展点
  - 持久化策略（per-task vs per-character）
  - 跨会话记忆边界
  - CustomCSS 沙箱
  - 角色卡导出格式
  - 降级模式触发条件
  - 叙事图 schema
  - 状态变更审计留存

## 归档

- 决策日期：2026-10-06
- 调研输入：afengy.com（含创作指南）、SillyTavern、RisuAI、Inworld、Convai、Hidden Door、Charisma.ai、NovelAI、character.ai、JanitorAI、SpicyChat、筑梦岛等
- 主要产出文档：
  - `docs/working/rp-game-engine-with-llm-enhancement.md`（详细设计）
  - `docs/working/afengy-creator-guide.md`（创作指南原文）
  - `docs/working/afengy-creator-guide-raw.html`（原始 HTML）
- 状态：**已采纳**，进入实施路线 Phase 1（M1）