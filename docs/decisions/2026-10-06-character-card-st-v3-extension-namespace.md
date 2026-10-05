# ADR 2026-10-06：角色卡格式 = SillyTavern V3 + AgentOS 扩展命名空间

## 背景

之前几轮迭代，角色卡格式设计经历了：
1. ADR `2026-10-06-rp-engine-first-architecture.md` —— **自创** AgentOS V1 格式（YAML / 自定字段）
2. ADR `2026-10-06-rp-two-axis-mode-spectrum.md` —— 仍沿用自创格式
3. ADR `2026-10-06-rp-sillytavern-ecosystem-compatibility.md` —— 提出兼容 ST，但仍是"自创格式 + 转换层"

用户最新反馈明确指出：

> "格式就使用酒馆只是可以扩展。"

**关键洞察**：

1. **不发明新格式** —— 直接用 SillyTavern V3 JSON 作为角色卡格式
2. **扩展** —— ST V3 已经在 `data.extensions` 字段预留"任意扩展"位置，AgentOS 把自己的扩展放在那里
3. **无转换层** —— 不需要"AgentOS 内部 YAML ↔ ST V3 JSON"的双向转换，**单一格式即可**

## 决策

**角色卡的唯一格式 = SillyTavern V3 JSON + AgentOS 扩展（挂在 `data.extensions.agentos` 命名空间下）**。

具体含义：

### 1. 格式选择

**用 ST V3，不用自创**：

| 维度 | 自创格式 | **ST V3 + AgentOS 扩展**（采纳） |
|---|---|---|
| 格式 | ST V3 | ST V3 |
| 扩展位置 | `data.extensions.{key}` | `data.extensions.agentos.*` |
| 兼容性 | 需转换层 | **原生兼容** |
| 字节级往返 | ✗ | ✓ |
| 学习成本 | 用户学新格式 | 用户认识 ST |
| 迁移成本 | 用户学转换 | **零** |

### 3. 命名空间规则

```
data.extensions.agentos
        ↓
   engine, variables, actions, lorebook, narrative_graph, rules, ui, llm_rules
```

**所有 AgentOS 扩展** 都在 `data.extensions.agentos.*` 下，**避免与 ST 未来扩展字段冲突**。

### 4. 字段映射

| ST V3 字段 | AgentOS 用途 |
|---|---|
| `data.name`, `description`, `personality`, `scenario`, `first_mes`, `mes_example` | LLM 看的人设（无变化） |
| `data.system_prompt`, `post_history_instructions` | LLM 系统提示（无变化） |
| `data.tags`, `creator`, `character_version` | 元数据（无变化） |
| `data.alternate_greetings[]` | 多开场白（无变化） |
| `data.creator_notes` | 作者笔记（无变化） |
| `data.creator_notes` | 作者笔记（无变化） |
| `data.extensions.talkativeness` | ST 用（AgentOS 不改） |
| **`data.extensions.agentos`** | **AgentOS 专用扩展**（ST 忽略） |

### 5. AgentOS 扩展的子结构

```yaml
data.extensions.agentos:
  version: "1.0.0"            # AgentOS 扩展 schema 版本
  engine:                     # 二轴声明 + author authority
    llm: "with_llm"
    rule_complexity: "core"
    allow_cheat: true
    contract_level: 2
    cheat_overrides: {...}
  variables:                  # 状态机 schema
    player: { hp: {...}, mp: {...} }
    npcs: { abigail: {...} }
    world: { time_of_day: {...} }
  rules:                      # 规则引擎配置
    combat: { formula: "..." }
    random_events: {...}
  actions:                    # Action 协议
    - { id: "attack", ... }
    - { id: "give_gift", ... }
  lorebook:                   # 世界书（含兼容 ST 字段 + AgentOS 扩展）
    entries: [{ uid, key, content, ..., extensions: { agentos: {...} } }]
  narrative_graph:            # 叙事图
    nodes: [...]
    schedule: [...]
    heart_events: [...]
    triggers: [...]
  ui:                         # UI 配置
    custom_css, bg_image, bgm, status_bar_template, rule_badge
  llm_rules:                  # LLM 拼装规则
    contract_level
    flavor_prompt_injection
```

### 6. 为什么不用自创格式

之前三轮 ADR 都试图"自创 AgentOS V1 格式"。这次决定废弃：

| 自创格式的问题 | 损失 |
|---|---|
| 用户学新格式 | 迁移成本 |
| ST 资产需要转换 | 字节级不往返（可能丢字段） |
| 创作者在 ST 中做，在 AgentOS 中改 | 兼容性差 |
| 维护两个格式的 spec | 工作量大 |
| 与 ST 社区脱节 | 失去生态 |

**正确做法**：用 ST V3 作为事实标准 + 命名空间扩展。

## Alternatives Considered

### A. 自创 AgentOS V1 格式（YAML / JSON）

- 优点：可自由设计
- 缺点：用户迁移成本高、与 ST 脱节、需要双向转换层
- 否决理由：用户反馈明确指出"格式就使用酒馆"

### B. 转换层（AgentOS YAML ↔ ST V3 JSON）

- 优点：内部用 YAML 方便，外部用 ST V3 兼容
- 缺点：转换层 bug 风险、字段可能丢、用户困惑（"为什么我得用 ST 格式？"）
- 否决理由：用户指出"只是可以扩展"，不需要转换

### C. ST V3 + AgentOS 扩展在 `data.extensions.agentos.*`（采纳）

- 优点：
  - 字节级往返（无信息丢失）
  - ST 与 AgentOS 共享同一份 JSON
  - ST 用户零迁移成本
  - AgentOS 扩展字段独立演进（用 `agentos.*` 命名空间隔离）
- 缺点：
  - 失去"自创格式的话语权"
  - ST 升级时需跟随
- 缓解：
  - ST V3 升级不破坏 AgentOS 扩展（命名空间隔离）
  - AgentOS 扩展 schema 自带版本号（`version: "1.0.0"`）

### D. 用 ST V3 但 AgentOS 扩展放在平级（不嵌套）

- 优点：扁平
- 缺点：与 ST 未来扩展冲突风险
- 否决理由：嵌套命名空间更安全

## 影响

### 正面

- **零迁移成本** —— ST 角色卡 = AgentOS 角色卡
- **字节级往返** —— 导入导出无信息丢失
- **生态共享** —— 任何 ST 资产（包括 SillyTavern 官方角色库）直接可用
- **命名空间隔离** —— ST V3 升级时 AgentOS 扩展不受影响
- **实现简化** —— 无需双格式 + 转换层

### 负面

- **JSON 格式** —— 创作者必须熟悉 JSON（之前 YAML 更友好）
- **ST 升级风险** —— V4 等大版本变化时需更新
- **AgentOS 扩展需版本管理** —— 自带 `version` 字段

### 中性

- 与现有 ST 工具链（PNG tEXt chunk 读写）可直接用
- 与 STscript 兼容层（ADR 4）配合使用

## 依赖与解锁

依赖：M0（ST 兼容）—— **这个 ADR 让 M0 的 PNG 读写逻辑也服务于 AgentOS 内部存储**，避免双格式。

解锁：
- M0 直接使用 ST V3 作为角色卡格式
- 创作者工具基于 ST V3 PNG
- 角色卡编辑器（Web）直接编辑 JSON
- 角色卡 lint 工具基于 ST V3 schema + AgentOS extension schema

## 关键约束

### A. 不允许破坏 ST 标准字段

`data.name` / `data.description` / `data.personality` 等 ST 标准字段由 ST 拥有，**AgentOS 不改这些字段的含义**。LLM 看这些字段时按 ST 语义。

### B. AgentOS 扩展必须在 `data.extensions.agentos`

任何 AgentOS 扩展字段必须挂在 `data.extensions.agentos.*` 下。**不允许污染 ST 标准字段或顶层 `data.extensions` 顶层字段**（如 `data.extensions.talkativeness`）。

### C. 版本管理独立

- `spec: "chara-card-v3"` —— ST 规范版本（升级 ST 时改）
- `spec_version: "3.0"` —— ST spec version（升级 ST 时改）
- `data.extensions.agentos.version: "1.0.0"` —— **AgentOS 扩展版本**（升级 AgentOS 时改）

AgentOS 升级扩展字段时**只改 `extensions.agentos.version`**，不动 ST 标准字段。

### D. PNG 读写策略

ST V3 卡通常用 PNG 嵌 JSON。AgentOS 的 PNG 读写策略：

1. **完整保留 PNG 字节** —— AgentOS 不修改 PNG 字节（除 metadata 写入）
2. **ST V3 字段 tEXt chunk** —— "chara" 字段保留 ST 完整 JSON
3. **AgentOS 扩展同样在 chara 字段** —— ST 看到 extensions.agentos 时直接忽略
4. **不写 AgentOS 专有 chunk** —— 避免破坏 PNG 兼容

## 用户反馈原话

> "格式就使用酒馆只是可以扩展。"

**关键洞察**：

1. **格式 = 酒馆**（SillyTavern V3）
2. **只是可以扩展**（AgentOS 在 ST V3 的 `extensions` 字段扩展）

## 归档

- 决策日期：2026-10-06
- 主要产出文档：
  - `docs/working/rp-game-engine-with-llm-enhancement.md`（§1 全部重写）
  - 本 ADR
- 状态：**已采纳**
- 后续行动：
  - M0 直接使用 ST V3 PNG 读写
  - 创作者工具基于 JSON 而非 YAML
  - 角色卡编辑器（Web）直接编辑 ST V3 JSON