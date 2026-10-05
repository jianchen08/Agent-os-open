---
title: AgentOS 角色扮演子系统设计：以游戏引擎为基础的 LLM 增强层
status: complete
created: 2026-10-06
updated: 2026-10-06
tags: [agentos, 角色扮演, 游戏引擎, LLM, 状态机, 规则引擎, 叙事图]
related:
  - afengy-creator-guide.md
---

# AgentOS 角色扮演子系统设计

> **核心定位**：AgentOS 角色扮演子系统 = **「酒馆（SillyTavern）的增强」**。
>
> SillyTavern（中文俗称「酒馆」）是全球最大的角色扮演前端生态：V2/V3 PNG 角色卡、Lorebook、Regex、STscript、Slash Commands、Group Chat、Variables、Extensions。AgentOS **不取代 SillyTavern**，而是：
>
> 1. **完全兼容 ST 资产** —— 导入导出 V2/V3 PNG 角色卡、STscript、Regex、Lorebook、Variables
> 2. **补齐 ST 缺的部分** —— 状态机 schema 化、叙事图、规则引擎、结构化输出、模式谱（无 LLM 也可玩）
> 3. **接入 AgentOS 内核生态** —— 持久化、agent_config_load、MCP 协议、Multi-agent、task system
> 4. **保留 ST 用户的创作习惯** —— prompt + Mod 写法继续可用（`none + with_llm` cell）
>
> 用户路径：SillyTavern 用户 → 导入 PNG 到 AgentOS → 选择 "开启规则引擎" → 享受结构化增强。
>
> ---
>
> ## 设计概要（二轴谱 + ST 兼容）
>
> 提供**两个独立可调的轴**，由角色卡与玩家按需声明，不强推任何单一路线：
>
> | 轴 | 取值 | 含义 |
> |---|---|---|
> | **LLM 可用性** | `with_llm` / `without_llm` | 是否调用 LLM |
> | **规则复杂度** | `none` / `core` / `full` | 创作者声明多少规则（连续可调） |
>
> **轴 1（LLM 可用性）**：
> - **`with_llm`**：常规模式，调用 LLM 生成叙事
> - **`without_llm`**：LLM 不可用 / 离线 / 节省时使用。**这一档不是完整的 RPG**，而是一个**小型流程化角色扮演游戏**（visual novel / 选择剧情）—— 叙事图走预设节点，文本用占位符模板拼装，状态机仍工作但纯确定性。够"小游戏"用，不追求 CRPG 复杂度。
>
> **轴 2（规则复杂度）**：
> - **`none`**：0 规则 → 等价 afengy 现状，LLM 自维护状态（HTML 状态栏、Mod、jailbreak 等）
> - **`core`**：只声明基础规则（HP/MP/位置/时间/基础战斗公式）→ **这是 AgentOS 默认模式**。LLM 写叙事，**常规状态由代码决定**；只有"模糊状态"（好感、心情、剧情冲突）由 LLM 自由发挥
> - **`full`**：声明一切规则（叙事图、世界书、战斗、事件、好感判定等）→ Convai 风格，最强结构化
>
> **关键设计原则**：
> 1. **常规状态永远由代码决定** —— HP、MP、位置、时间、基础战斗数值：这是角色卡上的"客观事实"。即使规则复杂度为 `none`，只要声明了，就由代码持有；LLM 不能自由改（这是与 afengy 的关键差异）
> 2. **模糊状态可由 LLM 自由发挥** —— 好感度变化、剧情冲突、NPC 情绪细节：LLM 在 `core` 模式下可自由输出（输出到 HTML 状态栏或专门的 `<narrative_state>` 块），代码层不校验
> 3. **规则是连续变量而非离散开关** —— 创作者声明的规则可多可少，自动适配到对应"模式"。`core` 与 `full` 之间没有硬边界
> 4. **契约松紧可调** —— 输出格式不是单一 JSON Schema，而是由创作者声明的**结构化等级（contract level）**：从纯文本到全结构化，每一档都是合法选项
> 5. **记忆复用 AgentOS 现有基础设施** —— 不重新发明轮子（详见 §8）
> 6. **SillyTavern 资产完全兼容** —— V2/V3 PNG、STscript、Regex、Lorebook、Variables、Slash Commands 必须 100% 可用（详见 §X）
>
> ## 目录

0. [设计哲学与架构原则](#0-设计哲学与架构原则)
1. [角色卡标准（Character Card）](#1-角色卡标准character-card)
1.5. [SillyTavern 生态兼容与增强](#15-sillytavern-生态兼容与增强)
2. [游戏引擎核心：状态机子系统](#2-游戏引擎核心状态机子系统)
3. [世界书 / Lorebook 子系统](#3-世界书--lorebook-子系统)
4. [叙事图（Narrative Graph）](#4-叙事图narrative-graph)
5. [规则引擎（战斗 / 数值 / 事件）](#5-规则引擎战斗--数值--事件)
6. [LLM 增强层（多模式可调契约）](#6-llm-增强层多模式可调契约)
7. [Action 协议（代码执行 LLM 的指令）](#7-action-协议代码执行-llm-的指令)
8. [记忆系统（三层架构）](#8-记忆系统三层架构)
9. [UI 与可视化](#9-ui-与可视化)
10. [AgentOS 插件蓝图](#10-agentos-插件蓝图)
11. [三模式谱：可切换、可降级](#11-三模式谱可切换可降级)
12. [与 afengy.com 创作指南逐条对照](#12-与-afengycom-创作指南逐条对照)
13. [实施路线](#13-实施路线)
14. [ADR 决策清单](#14-adr-决策清单)
15. [测试与质量保障](#15-测试与质量保障)

---

## 0. 设计哲学与架构原则

### 0.1 设计公理

1. **二轴独立可调** —— LLM 可用性（with/without）和规则复杂度（none/core/full）是两个独立轴。角色卡声明当前轴上的位置，玩家可运行时切换轴上的滑点。**没有"模式"枚举**，只有连续的位置。
2. **常规状态由代码决定** —— HP、MP、位置、时间、基础战斗数值：声明了就别少，不允许 LLM 自由改。这是与 afengy 的根本区别。
3. **模糊状态由 LLM 自由发挥** —— 好感度变化、剧情冲突、NPC 情绪细节：LLM 在 `core` 模式下可自由输出（写到 HTML 状态栏或专门的 `<narrative_state>` 块），代码层不校验；`full` 模式下走 schema 提议 → 校验 → 落库。
4. **LLM 的输出契约是「宽进严出」** —— LLM 可以输出任意自然语言，但**能被解析成什么**是契约说了算。契约有等级（见 §6），创作者/玩家选档；架构容忍「契约失败时退回宽松模式」。
5. **记忆复用 AgentOS 现有基础设施** —— 不重建轮子。AgentOS 0.2 已有 session 内存 / agent_config_load / 持久化内核，应按"内嵌"原则接入。

### 0.2 二轴 × 二档完整图

```
                    LLM 可用性
                  with_llm            without_llm
规则 none    ┌──────────────────┐  ┌──────────────────┐
复杂度       │ 等价 afengy 现状 │  │ 无意义（不可用）  │
             │ LLM 全权         │  │ 跳过              │
             └──────────────────┘  └──────────────────┘

规则 core    ┌──────────────────┐  ┌──────────────────┐
（默认）      │ ★ AgentOS 默认模式│  │ ★ 离线小型流程游戏│
             │ 常规状态 = 代码    │  │ narrative_graph + │
             │ 模糊状态 = LLM    │  │ 模板叙事          │
             └──────────────────┘  └──────────────────┘

规则 full    ┌──────────────────┐  ┌──────────────────┐
             │ Convai 风格       │  │ 离线完整游戏      │
             │ 全部状态 = 代码    │  │ visual novel-lite │
             │ LLM 只生成叙事    │  │                  │
             └──────────────────┘  └──────────────────┘
```

★ **两个核心 cell**：
- **`core + with_llm`**：AgentOS 默认推荐模式。LLM 写故事，常规游戏状态（HP/位置/时间/基础战斗）由代码持有——这是与 afengy 现状的本质差异。
- **`core + without_llm`**：LLM 不可用时的降级形态：**一个可独立运行的小型流程化游戏**（visual novel / 文字冒险），不需要玩家会写 prompt，玩家点选项推进剧情。

### 0.3 「常规状态 vs 模糊状态」明确边界

**常规状态**（always code-enforced）—— 创作者在 `engine.variables` 声明，**任何模式都不能被 LLM 改**：

| 字段 | 原因 |
|---|---|
| `player.hp, mp, gold, stamina` | 数值化、有上限、需要精确 |
| `world.location, time_of_day, weather` | 离散状态、影响事件触发 |
| `player.inventory` | 物品数量与唯一性必须强校验 |
| `tasks.completed[], progress[]` | 任务进度需要持久化 |

**模糊状态**（LLM 自由发挥；`full` 模式才校验）：

| 字段 | 原因 |
|---|---|
| `npcs.X.affection, .mood, .attitude` | 情感维度，LLM 写得最自然 |
| `narrative.conflict, .secret, .revealed` | 剧情变量，本来就是叙事一部分 |
| `narrative.tone, .pace, .themes[]` | 文学性维度，schema 化会失味 |

> 设计要点：**模糊状态本身就是叙事的一部分**，强行让 LLM 输出 `affection +15` 而不属于 narration 流，会破坏体验。**让 LLM 在 narration 里直接说"好感提升"**，前端按需解析（HTML `<details>` 标签或模糊正则），比强行 schema 更自然。

### 0.4 角色卡声明

```yaml
character_card:
  spec: "agentos-character-card-v1"
  spec_version: "1.0.0"
  
  engine:
    # === 二轴声明 ===
    llm: "with_llm"                # "with_llm" | "without_llm"
    rule_complexity: "core"         # "none" | "core" | "full"
    
    # === 运行时切换偏好 ===
    allow_runtime_switch: true      # 允许玩家切轴上的滑点
    fallbacks:
      on_llm_unavailable: "without_llm"   # LLM 挂时降级
      on_engine_failure: "with_llm:core"  # 引擎故障时降级
    
    # === 契约松紧档（按模式自动选默认值） ===
    contract_level: 2               # 见 §6
    
    # === 常规状态：永远由代码持有 ===
    variables:
      player:
        hp:   { type: int, default: 100, min: 0, max: 100 }
        mp:   { type: int, default: 50,  min: 0, max: 50 }
        gold: { type: int, default: 0 }
      world:
        location:    { type: enum, values: [剑冢, 集市, 客栈, 山门] }
        time_of_day: { type: enum, values: [子时..亥时], default: 申时 }
    
    # === 基础战斗规则（可选） ===
    combat:
      formula: "atk * (1 + rand(-0.1, 0.1)) - def * 0.5"
    
    # === 叙事图（`full` 模式可选）===
    narrative_graph:
      nodes: [...]
      events: []
    
    # === LLM 拼装的 prompt 元素（无则省略）===
    lorebook:
      entries: []
    
    # === UI 配置 ===
    ui:
      status_bar_template: |
        <div class="status-bar">
          HP: {{ state.player.hp }}/100 | {{ state.world.location }}
          {% if state.narrative %}
          | {{ state.narrative | safe }}    {# LLM 自由输出的模糊状态 #}
          {% endif %}
        </div>
```

### 0.5 合法切换路径

```
                LLM 健康度变化 / 用户偏好变化
                          ▼
    with_llm(core)  ◄──────►  without_llm(core)
        ▲   │                      ▲   │
        │   ▼                      │   ▼
    with_llm(full)  ◄──────►  without_llm(full)
        ▲   
        │  用户主动开启更多规则
        ▼
     with_llm(none)  =  afengy 等价物
```

- **`core` ↔ `full`**：玩家可一键开关「启用严格规则」
- **`with` ↔ `without_llm`**：LLM 健康度触发自动降级，或玩家切离线模式
- **`core ↔ none`**：玩家主动关闭引擎，LLM 接管一切（afengy 模式）
- **`full → none`**：被禁止——一旦声明了规则就不能脱钩（防止玩家作弊）

### 0.6 术语表

| 术语 | 含义 |
|---|---|
| **LLM Rule** | 角色卡顶部 `llm` 轴声明（with/without） |
| **Rule Complexity** | `engine.rule_complexity`（none / core / full） |
| **Engine Tick** | 引擎一回合：玩家输入 → 解析 → 规则引擎 → LLM（可选）→ Action → 状态落库 |
| **Regular State** | 常规状态：永远由代码持有的字段（HP/位置/时间/物品/任务） |
| **Fuzzy State** | 模糊状态：LLM 自由发挥的字段（好感/剧情冲突/文学维度） |
| **Schema** | 状态变量声明（类型、范围、tag）—— 只描述常规状态 |
| **Narrative Graph** | 叙事图：节点（目标）+ 边（决策）+ 触发器（`full` 模式） |
| **Trigger** | 触发器：位置/时间/事件/阈值 → 切换节点 |
| **Action** | 动作：可由客户端执行的原子操作 |
| **Contract Level** | 输出契约松紧档：0=纯文本 / 1=+choices / 2=+actions / 3=+state_changes |
| **Fallback Path** | 不可用时的降级路径 |

---

## 1. 角色卡标准（Character Card）

### 1.1 角色卡哲学

角色卡不是"一段 prompt"，而是一个**声明游戏规则的文档**——告诉系统：

- LLM 是否调用（轴 1）
- 多少规则（轴 2）
- 哪些字段是常规状态（代码持有）
- 哪些字段是模糊状态（LLM 自由发挥）
- 哪些 prompt 元素要喂给 LLM

LLM 看到这张卡，理解的是"我要为这个游戏的当前快照生成一段文字，**不要写常规状态栏**——那是代码的事——专心写叙事和模糊情感"。

### 1.2 角色卡结构（AgentOS 标准 V1）

```yaml
# character_card.yaml
character_card:
  spec: "agentos-character-card-v1"
  spec_version: "1.0.0"
  
  # === 身份信息 ===
  meta:
    id: "uuid-v4"
    name: "苏清婉"
    author: "author_id"
    version: "1.2.3"
    cover: "./cover.jpg"
    tags: ["古风", "修仙", "女侠"]
    
  # === 文学/风格信息（仅给 LLM 看的） ===
  flavor:
    scenario: "暮色四合，你在剑冢洞口遇到一个白衣女子。"  # 开场白
    title: "剑冢奇缘"
    description_short: "你失去了过去的20天记忆，而眼前这个自称了解一切的人..."  # 列表页展示
    description_long: "..."  # 详情页 HTML
    style_guide: |
      文言白话混合，避免现代词汇。动作描写克制，对话精炼。
    
  # === 引擎核心：游戏规则定义 ===
  engine:
    # 1. 状态变量声明（Game Schema）
    variables:
      player:
        hp:           { type: int,   default: 100, min: 0, max: 100 }
        mp:           { type: int,   default: 50,  min: 0, max: 50 }
        gold:         { type: int,   default: 0 }
        inventory:    { type: list,  item_type: { id: str, qty: int } }
      npcs:
        suqingwan:
          affection:  { type: int, default: 20, min: 0, max: 100, tags: [relationship] }
          trust:      { type: int, default: 0,  min: 0, max: 100 }
          location:   { type: enum, values: [剑冢, 集市, 客栈, 山门], default: 剑冢 }
          mood:       { type: enum, values: [neutral, wary, gentle, angry], default: neutral }
      world:
        time_of_day:  { type: enum, values: [子时, 丑时, ..., 亥时], default: 申时 }
        weather:      { type: enum, values: [晴, 阴, 雨, 雪], default: 晴 }
        location:     { type: enum, values: [剑冢, 集市, 客栈, 山门], default: 剑冢 }
        flags:        { type: dict, key_type: str, value_type: bool }
    
    # 2. 可用 Action 清单
    actions:
      - id: "attack"
        description: "用武器攻击目标"
        params: { target: str, weapon: str }
        handler: "agentos.rp.combat:resolve_attack"   # 代码层处理
      - id: "give_item"
        params: { item_id: str, target_npc: str }
        handler: "agentos.rp.items:give"
      - id: "talk"
        params: { text: str, target_npc: str }
        handler: "agentos.rp.dialog:talk"
    
    # 3. 世界书（按需加载）
    lorebook:
      entries:
        - id: "npc_old_zhou"
          keywords: ["老周", "周队长"]
          logic: "OR"
          scan_depth: 4
          position: "after_system"
          content: "老周，52岁，铁壁营地领袖..."
    
    # 4. 叙事图（可选）
    narrative_graph:
      nodes:
        - id: "intro"
          title: "初遇"
          on_enter: "state.npcs.suqingwan.affection >= 10"
          objectives: ["完成与苏清婉的第一次对话"]
        - id: "branch_a"
          title: "追问真相"
          decisions:
            - when: "player.text matches /你过去/"
              next: "deep_dive"
      triggers:
        - type: "threshold"
          when: "state.npcs.suqingwan.affection >= 90"
          jump_to: "love_confession"
    
    # 5. 规则引擎配置（战斗、掉落、随机事件）
    rules:
      combat:
        formula: "atk * (1 + rand(-0.1,0.1)) - def * 0.5"
        crit_threshold: 0.1
        crit_mult: 2.0
      random_events:
        pool:
          - { id: "stranger_approach", weight: 0.2, cooldown_rounds: 5 }
          - { id: "rain_starts",       weight: 0.15 }
          - { id: "merchant_passby",   weight: 0.3 }
        trigger_every_n_turns: 4
    
    # 6. UI 配置
    ui:
      custom_css: "./style.css"
      bg_image: "./bg.jpg"
      bgm: "./theme.mp3"
      status_bar_template: |
        <div class="status-bar">
          {{ state.player.hp }} HP
          | {{ state.npcs.suqingwan.name }}: 好感 {{ state.npcs.suqingwan.affection }}
        </div>
  
  # === 创作者元数据 ===
  extensions:
    - "agentos.rp.narrative_graph"
    - "agentos.rp.combat"
    - "agentos.rp.lorebook_rag"
```

### 1.3 与 SillyTavern / Convai 的对比

| 维度 | SillyTavern V2/V3 | Convai | AgentOS V1 |
|---|---|---|---|
| 格式 | PNG 嵌 JSON | 平台私有 | YAML / JSON 双格式 |
| 状态变量 | 仅 `data.Variables`（运行时由扩展维护） | "Personality Traits" 滑块 | `engine.variables` 强 schema |
| Action | 无 | 平台专属 | `engine.actions` 显式声明 |
| 叙事图 | 无 | Narrative Design 独立子系统 | `engine.narrative_graph` |
| 规则 | 无 | 仅"复杂动作" | `engine.rules` 配置化 |
| 跨平台导入 | V2/V3 PNG 标准 | 私有 | **支持导入 V2 / V3 + 导出 PNG-V3 + JSON** |

### 1.4 创作者工具（CLI + Web）

```bash
agentos creator new --template xianxia              # 新建（基于模板）
agentos creator lint my_card.yaml                  # 静态检查
agentos creator test my_card.yaml --simulator      # 模拟 5 回合
agentos creator preview my_card.yaml               # 启动本地预览
agentos creator import-st <file.png>               # 从 SillyTavern PNG 导入
agentos creator export my_card.yaml --format png-v3 # 导出 PNG-V3
agentos creator publish my_card.yaml               # 打包发布到市场
```

---

## 1.5. SillyTavern 生态兼容与增强

> **本节是 §1 与后续 §2-§10 之间的桥梁。** 它定义 AgentOS **与 ST 的兼容边界**，以及**ST 缺什么、AgentOS 给什么**。

### 1.5.1 ST 是什么、AgentOS 与 ST 的关系

**SillyTavern**（中文俗称「酒馆」）：
- 全球最大的 LLM RP 前端生态
- 角色卡 V2/V3 PNG 标准（事实标准）
- Lorebook（关键词触发世界书）
- Regex Scripts（前后处理）
- STscript（JS 脚本引擎）
- Slash Commands（聊天内命令）
- Group Chat（多角色房间）
- Variables（脚本维护的变量）
- Extensions（社区插件）
- Data Bank（RAG 文档库）

**AgentOS 与 ST 的关系**：

```
SillyTavern  ←→  兼容层  ←→  AgentOS 增强
 (现状 100% 可用)        (补齐 ST 缺的部分)
```

**兼容层** = 读 / 写 ST 资产文件、与 ST 用户无缝迁移。
**增强** = 在 ST 之上加：状态机 schema、叙事图、规则引擎、结构化输出、模式谱、AgentOS 内核生态。

### 1.5.2 ST 资产兼容性清单

| ST 资产 | 兼容性 | 备注 |
|---|---|---|
| **角色卡 V2** | ✓ 100% 读取 + 100% 写出 | PNG tEXt chunk 中 chara 字段 |
| **角色卡 V3** | ✓ 100% 读取 + 100% 写出 | PNG tEXt chunk 中 chara + V3 扩展字段 |
| **Lorebook（World Info）** | ✓ 100% 读取 + 写出 | ST 关键词 / 触发 / 优先级 / 递归扫描全部支持 |
| **Regex Scripts** | ✓ 100% 读取 + 写出 + 兼容执行 | ST 的正则脚本语法直接运行 |
| **STscript** | ✓ 读取 + 编译执行 | ST 的 `/cmd` 命令在 AgentOS chat 中识别 |
| **Slash Commands** | ✓ 100% 识别 | `/sys`, `/let`, `/add`, `/setvar` 等原生支持 |
| **Group Chat** | ✓ 100% 兼容 | 多角色房间直接迁移 |
| **Variables** | ✓ 100% 兼容 + **提升**（见 §1.5.3） | STscript 维护的 variables 在 AgentOS 中由状态机持有 |
| **Data Bank (RAG)** | ✓ 100% 兼容 + **提升**（见 §1.5.4） | AgentOS 提供更稳定的本地嵌入 |
| **Extensions** | ✓ 加载兼容层 + **原生增强**（见 §1.5.5） | ST extension 能在 AgentOS 中跑；AgentOS 提供更稳的 SDK |

### 1.5.3 ST Variables → AgentOS 状态机（核心增强）

**ST 的痛点**：Variables 由 STscript 维护，靠作者写脚本 set/get，不声明类型，容易漂移。

**AgentOS 的增强**：导入 ST 角色卡时，**自动扫描 STscript 中的 variable 操作**，推断 schema 建议：

```python
# STscript 中作者写的：
# /let affection = 0
# /let hp = 100
# /add affection 5

# AgentOS 导入时自动推断：
inferred_schema = {
    'affection': {'type': 'int', 'default': 0, 'min': 0, 'max': 100},
    'hp':        {'type': 'int', 'default': 100, 'min': 0, 'max': 100},
}
# 写入角色卡 agentos.variables[] 中
# 同时保留原 STscript（用户可手动修正 schema）
```

**结果**：ST 用户的脚本继续跑（STscript 兼容层），但 AgentOS 提供**强 schema 校验**作为增强。

### 1.5.4 ST Data Bank → AgentOS RAG（核心增强）

**ST 的现状**：Data Bank 依赖云端嵌入 API，本地嵌入支持弱。

**AgentOS 的增强**：
- 内置本地嵌入（FastEmbed / BGE 等）
- 支持**关键词 + 嵌入双轨召回**（ST 仅关键词）
- 提供 lorebook_rag 插件（详见 §3）

**ST 资产导入后**，AgentOS 自动接管 Data Bank，用更稳的本地检索。

### 1.5.5 ST Extension → AgentOS 插件（生态升级）

**ST Extension 的特点**：JavaScript 写，无类型约束，依赖 ST 内部 API。

**AgentOS 的兼容策略**：

| 阶段 | 兼容性 |
|---|---|
| **M1（MVP）** | ST Extension 通过 `agentos.rp.st.compat` 适配层加载；提供 ST 内部 API 的 stub |
| **M2** | 鼓励 ST 作者迁移到 AgentOS 原生插件（Python，更稳） |
| **M3** | ST Extension 进入维护模式，新功能走 AgentOS 原生 |

**ST Extension 适配层示例**（M1 实现）：

```python
# plugins/shared/rp/st_compat/loader.py

class STExtensionLoader:
    """加载 ST 的 JS Extension，提供 ST 内部 API stub"""

    def __init__(self, agentos_kernel, llm_invoker):
        self.kernel = agentos_kernel
        self.llm = llm_invoker
        # ST 内部 API stub（最小集）
        self.st_api = {
            'getContext': self._stub_get_context,
            'setExtensionPrompt': self._stub_set_extension_prompt,
            'injectScript': self._stub_inject_script,
            'eventOn': self._stub_event_on,
            'eventOff': self._stub_event_off,
            # ... 大约 30+ 个 ST API 的 stub
        }

    def load(self, extension_path: str):
        """加载 ST 扩展的 JS 代码，在 stub 化的 ST API 环境中运行"""
        js_code = read_file(extension_path)
        # 用 quickjs 或 pyexecjs 执行
        runtime = QuickJSRuntime()
        runtime.set_global('ST', self.st_api)
        runtime.set_global('AgentOS_Rpc', self.kernel.get_kernels())
        runtime.execute(js_code)
```

### 1.5.6 ST 用户路径（M1 即可用）

```bash
# 1. ST 用户已有角色卡（V2/V3 PNG）
ls ~/SillyTavern/data/default/user/characters/
#   - 仙侠角色.png
#   - 现代角色.png

# 2. 一键导入
agentos rp import ~/SillyTavern/data/default/user/characters/仙侠角色.png
# 自动转换：V3 JSON → AgentOS YAML 角色卡
# 自动推断：variables schema、lorebook、regex
# 输出：~/.agentos/characters/xianxia.yaml

# 4. 选择是否开启"结构化增强"
agentos rp upgrade xianxia.yaml
# 提供：
#   1. 保留 STscript + 加 schema 校验（推荐）
#   2. 保留 STscript + 加叙事图（可选）
#   3. 完全迁移到 AgentOS 原生（高级）

# 5. 启动 AgentOS 预览
agentos creator preview xianxia.yaml
# 浏览器打开：左侧 ST 风格 chat + 右侧状态栏
# 享受：HP/物品/任务不再漂移

# 6. 导出回 ST（可逆）
agentos rp export xianxia.yaml --format png-v3
# 输出：xianxia-agentos-enhanced.png
# 在 SillyTavern 中可继续使用（兼容）
```

### 1.5.7 ST 用户的"渐进增强"路径

| 阶段 | 改动 | 体验 |
|---|---|---|
| **0. 导入即用** | 一键导入 ST 角色卡 | 与 ST 100% 一致 |
| **1. 开启 schema** | 一键升级 → 加 schema 校验 | HP/位置不漂移；STscript 继续跑 |
| **2. 开启叙事图** | 加 `engine.narrative_graph` | 分支不再漂移；模板叙事兜底 |
| **3. 开启规则引擎** | 加 `engine.rules.combat` | 战斗确定性；掉落/事件可控 |
| **4. 完全原生** | 把 STscript 翻译成 AgentOS 原生插件 | 性能 + 稳定性 + MCP 接入 |

**关键**：每一步都是**可逆的、向后兼容的**。用户不需要一次迁移。

### 1.5.8 AgentOS 给 ST 的"补强"清单

| ST 有 | ST 没有 | AgentOS 提供 |
|---|---|---|
| V2/V3 PNG 角色卡 | **状态机 schema 化** | `engine.variables` 强类型 |
| Lorebook | **阈值 / 场景 / 任务 / 正则触发** | 5 种触发模式 |
| Regex | **结构化输出（pre-parser）** | `contract_level` 4 档 |
| STscript（JS） | **强 schema 校验** | State Contract Validator |
| Slash Commands | **叙事图（节点 + 决策 + 触发器）** | `engine.narrative_graph` |
| Variables（弱类型） | **持久化保证** | 内核 SQLite 持久化 |
| Data Bank（云端嵌入） | **本地稳定嵌入** | FastEmbed / BGE 等 |
| Group Chat | **自动 NPC 角色切换** | Action 协议 |
| 无模式谱（必须有 LLM） | **无 LLM 也能玩** | 模式谱（无 LLM = visual novel） |
| 无 MCP / 多 Agent | **AgentOS 多 Agent / MCP / 持久化** | 内核生态 |
| 无审计 / 重放 | **审计日志 + 会话重放** | 内核 session 复用 |

### 1.5.9 STscript 兼容子集（AgentOS 原生支持的 ST 指令）

| ST 指令 | AgentOS 实现 |
|---|---|
| `/set name = X` | ✓ 映射到 `state.X = ...` |
| `/add hp -5` | ✓ 映射到 schema 校验的状态变更 |
| `/let affection = X` | ✓ 推断 schema |
| `/getvar name` | ✓ 读 state |
| `/trigger X` | ✓ 跳到叙事图节点 |
| `/sys <prompt>` | ✓ 注入 system prompt 片段 |
| `/run <script>` | ✓ 调用 Python 插件 |
| `/?` | ✓ 帮助文档 |
| `/abort` | ✓ 中止当前回合 |
| `/continue` | ✓ 让 LLM 继续生成 |
| `/goonreply` | ✓ 同 ST |

### 1.5.10 STscript → AgentOS 原生插件的迁移路径

```python
# ST 用户的 STscript:
# /let hp = 100
# /let gold = 0
# /let location = "剑冢"
# /set hp = 100
# /add gold 5
# /trigger "地点：剑冢"

# AgentOS 等价的 character_card YAML:
character_card:
  engine:
    variables:
      player:
        hp:    { type: int, default: 100, min: 0, max: 100 }
        gold:  { type: int, default: 0 }
      world:
        location: { type: enum, values: [剑冢, 集市, 客栈] }
    narrative_graph:
      triggers:
        - when: "state.world.location == '剑冢'"
          jump_to: "剑冢_节点"

# 用户不必立刻迁移——STscript 在 AgentOS 中继续有效
# 但用 YAML 表达后，享受 schema 校验 + 持久化 + 可视化
```

### 1.5.11 与 ST 社区的协作建议

| 行动 | 价值 |
|---|---|
| **官方文档**加 ST 迁移指南 | ST 用户群（最大潜在用户）可无障碍切换 |
| **Discord/RSS 同步** ST 社区 | 让 ST 作者知道"AgentOS 是 ST 增强，不是替代" |
| **赞助 ST 上游** | 与社区共赢 |
| **接收 ST 贡献** | 鼓励 ST 作者在 AgentOS 中再发布 |

### 1.5.12 关键承诺

**对 ST 用户的承诺**：

1. **零迁移成本** —— 导入 ST 角色卡 = 在 AgentOS 中使用
2. **可逆迁移** —— 导出回 ST 仍可用
3. **增量增强** —— 一项项加引擎功能，不强推
4. **保留 prompt 习惯** —— ST 创作者的 prompt / Mod 继续生效
5. **保留脚本** —— STscript 直接运行
6. **ST 插件继续工作** —— 通过 compat 层加载

---

## 2. 游戏引擎核心：状态机子系统

### 2.1 状态机分类

| 类别 | 字段示例 | 持久化 |
|---|---|---|
| **玩家属性** | `hp, mp, gold, stamina, energy` | per-task |
| **位置/时间** | `world.location, world.time_of_day, world.weather` | 当前会话 |
| **物品/资源** | `player.inventory` | per-task |
| **关系网络** | `npcs[id].affection, .trust, .hostility` | per-task |
| **任务进度** | `quests[id].stage, .flags[]` | per-task |
| **剧情标记** | `flags[key] = bool` | per-task |
| **行为痕迹** | `visited[], killed[], romanced[]` | per-task |
| **跨会话档案** | `_id` 用户的统计 + 偏好 | per-user |

### 2.2 状态变更的四种来源（这是关键设计）

```
            ┌─────────────────┐
玩家输入 ───►│  1. Input Parser│──► 识别意图（攻击 / 移动 / 对话 / 物品）
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  2. Rules Engine│──► 跑战斗 / 随机事件 / 好感判定
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  3. State Trans │──► 计算 state_changes（确定性事实）
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  4. LLM Narrator│──► 拿到事实流，生成 narration + proposed_state_changes
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  5. Contract Val│──► 校验 LLM 提议（不越权 / 不超量 / 合法）
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  6. Action Exec │──► 执行 actions（动画/音效/UI 事件）
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │  7. State Commit│──► 落库（per-task_id in SQLite）
            └─────────────────┘
```

| 步骤 | 谁负责 | 不变量 |
|---|---|---|
| 1. Input Parser | 代码 | 玩家输入必须能被解析成已知 intent，否则走 LLM 兜底 |
| 2. Rules Engine | 代码 | 战斗/事件/掉落结果必须确定性（种子化随机） |
| 3. State Transaction | 代码 | state_changes 是 application 级别的确定性事件流 |
| 4. LLM Narrator | LLM | 只读 state，输出 narration + proposed_state_changes |
| 5. Contract Validator | 代码 | LLM 的提议必须经过 schema 校验才能 merge |
| 6. Action Executor | 代码 | 每个 action 一个 handler，失败不抛 |
| 7. State Commit | 代码 | 事务化（要么全成要么全败） |

**LLM 永远在第 4 步，前后都是代码**。这是「Engine-first」的核心。

### 2.3 状态变更契约（LLM 输出协议）

LLM 的输出必须是这个 JSON Schema（**严格固定，不允许破坏**）：

```json
{
  "narration": "苏清婉接过木剑，指尖微颤。",
  "narration_meta": {
    "mood": "gentle",
    "pace": "slow"
  },
  "proposed_state_changes": [
    {
      "path": "npcs.suqingwan.affection",
      "op": "+",
      "value": 8,
      "reason": "玩家赠剑，主动示好"
    }
  ],
  "suggested_actions": [
    { "type": "npc_emote", "target": "suqingwan", "emotion": "gentle" },
    { "type": "play_sfx",  "track": "soft_chime.ogg" }
  ],
  "suggested_choices": [
    { "id": "a", "text": "追问她为何变脸" },
    { "id": "b", "text": "默默跟上" }
  ],
  "thinking": "玩家提议建立信任，NPC 处于 70 好感区间...",
  "_metadata": { "model": "deepseek-v3.2", "tokens": 642 }
}
```

### 2.4 契约校验（State Contract Validator）

```python
# plugins/shared/rp/state_machine/contract_validator.py

class StateContractValidator:
    """LLM 输出 → 校验 → 落库"""

    def __init__(self, schema):
        self.schema = schema  # 角色卡的 engine.variables

    def validate_and_apply(self, current_state, proposed_changes, hard_rules):
        audit = []
        accepted = []
        rejected = []

        for c in proposed_changes:
            # 1. path 必须在 schema 声明过
            if c['path'] not in self.schema:
                rejected.append({'change': c, 'reason': 'unknown_path'})
                continue

            # 2. op 合法
            if c['op'] not in {'+', '-', '*', '/', '=', 'set', 'unset', 'toggle', 'append', 'remove'}:
                rejected.append({'change': c, 'reason': 'invalid_op'})
                continue

            # 3. 类型检查
            try:
                coerced = self.coerce(c['value'], self.schema[c['path']]['type'])
            except (TypeError, ValueError):
                rejected.append({'change': c, 'reason': 'type_mismatch'})
                continue

            # 4. 范围 clamp
            old = self.resolve(current_state, c['path'])
            new = self.apply(old, c['op'], coerced)

            sch = self.schema[c['path']]
            if 'min' in sch and new < sch['min']:
                audit.append({'type': 'clamp_low', 'path': c['path'], 'forced': new, 'limit': sch['min']})
                new = sch['min']
            if 'max' in sch and new > sch['max']:
                audit.append({'type': 'clamp_high', 'path': c['path'], 'forced': new, 'limit': sch['max']})
                new = sch['max']

            # 5. 硬规则校验（最大变化量、最大并行变化数等）
            delta = abs(new - old)
            max_delta = hard_rules.get(c['path'], {}).get('max_delta_per_turn')
            if max_delta is not None and delta > max_delta:
                audit.append({'type': 'rate_limited', 'path': c['path'], 'attempted': delta, 'limit': max_delta})
                new = old + math.copysign(max_delta, new - old)

            accepted.append({'path': c['path'], 'op': c['op'], 'value': new})

        return ValidationResult(accepted=accepted, rejected=rejected, audit=audit)

    def commit(self, state, accepted_changes):
        for c in accepted_changes:
            self.resolve(state, c['path'])  # ensure path exists
            self.apply(state, c['path'], c['op'], c['value'])
        return state
```

### 2.5 状态机代码（Python）

```python
# plugins/shared/rp/state_machine/engine.py

class StateMachine:
    """per-task 的状态机实例"""

    def __init__(self, task_id, schema, storage):
        self.task_id = task_id
        self.schema = schema
        self.storage = storage
        self.state = storage.load_state(task_id) or self._initial_state(schema)

    def _initial_state(self, schema):
        s = {}
        for path, sch in self.schema.items():
            self._set(s, path, sch.get('default'))
        return s

    def apply_validated(self, event):
        """应用一个已经被校验过的事务"""
        for c in event['accepted']:
            self._set(self.state, c['path'], self._apply_op(self._get(self.state, c['path']), c['op'], c['value']))
        self.storage.save_state(self.task_id, self.state, version=event.get('version'))

    def snapshot(self):
        """给 LLM 看的状态快照"""
        return {
            'player': self._get(self.state, 'player'),
            'npcs':   {k: self._get(self.state, f'npcs.{k}') for k in self._get(self.state, 'npcs', {})},
            'world':  self._get(self.state, 'world'),
        }

    def _get(self, root, path, default=None):
        parts = path.split('.')
        cur = root
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def _set(self, root, path, value):
        parts = path.split('.')
        cur = root
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def _apply_op(self, old, op, value):
        if op == '=':     return value
        if op == '+':     return old + value
        if op == '-':     return old - value
        if op == '*':     return old * value
        if op == '/':     return old / value
        if op == 'set':   return value
        if op == 'unset': return None
        if op == 'toggle': return not old
        if op == 'append':
            return (old or []) + [value]
        if op == 'remove':
            return [x for x in (old or []) if x != value]
```

### 2.6 反作弊规则清单

```yaml
# 角色卡 engine.rules 扩展
hard_rules:
  - path: "npcs.*.affection"
    max_delta_per_turn: 10      # 单回合好感变化不超过 ±10
  - path: "npcs.*.trust"
    max_delta_per_turn: 5
  - path: "world.*"
    max_delta_per_turn: 1       # 位置/时间单回合最多变 1 次
  - path: "player.*"
    require_player_action: true  # 玩家属性必须由玩家行为触发，LLM 不能直接改
  - global:
    max_changes_per_turn: 8      # LLM 提议最多 8 个 changes
    require_reason: true         # 每个 change 必须带 reason
    banned_paths:                # LLM 永远不能改的字段
      - "player.account_id"
      - "task.*"
      - "_internal.*"
```

---

## 3. 世界书 / Lorebook 子系统

### 3.1 条目结构

```yaml
lorebook_entries:
  - id: "npc_old_zhou"
    keywords: ["老周", "周队长", "营地领袖"]
    logic: "OR"                    # OR / AND / NOT / WEIGHT
    scan_depth: 6                  # 扫描最近 N 条消息
    position: "after_system"       # 插入位置
    enabled: true
    content: |
      老周，52岁，退役军人，铁壁营地的领导者……
    
    # === 引擎扩展 ===
    inject_to: "system"            # system / user / assistant
    cooldown_rounds: 0
    triggers:
      - type: "state"
        when: "world.location == '铁壁营地'"
      - type: "threshold"
        when: "npcs.suqingwan.affection >= 70"
    cost_estimate: 230
```

### 3.2 触发模式（5 种）

| 触发模式 | 触发条件 | 用途 |
|---|---|---|
| **关键词** | 默认 | afengy / SillyTavern 风格 |
| **场景** | `state.world.location == X` | 地点相关 NPC/描述 |
| **任务** | `state.quests[id].stage == X` | 任务阶段相关设定 |
| **阈值** | `state.npcs[id].affection >= X` | 解锁隐藏背景 |
| **正则** | `player.text matches /regex/i` | 高级；识别玩家输入语义 |

### 3.3 智能加载：嵌入检索（RAG 模式）

对超大世界观（>200 条目）启用 RAG：
1. 条目内容向量化（嵌入模型本地或远程）
2. 玩家输入 + 最近对话 → 检索 Top-K
3. HyDE（生成假设性回答 → 检索）可优化
4. 用 cross-encoder 精排

实现为可选插件 `agentos.rp.lorebook_rag`，创作者可声明启用。

### 3.4 Token 估算（创作端 lint）

```python
# 静态分析：评估每回合大致 token 消耗
def estimate_lorebook_cost(entries, avg_scan_depth=4):
    triggered_per_turn = []
    for _ in range(simulate_n_turns := 1000):
        triggered = [e for e in entries if e.matches(recent_msgs)]
        triggered_per_turn.append(sum(len(tokenize(e.content)) for e in triggered))
    return {
        'avg': statistics.mean(triggered_per_turn),
        'p50': statistics.median(triggered_per_turn),
        'p95': statistics.quantiles(triggered_per_turn, n=20)[18],
        'p99': statistics.quantiles(triggered_per_turn, n=100)[98],
    }
```

**灵感来源**：afengy 创作指南明确指出「世界书的核心价值是按需加载节省 token」，我们把这条规约代码化。

---

## 4. 叙事图（Narrative Graph）

### 4.1 数据结构

```yaml
narrative_graph:
  nodes:
    - id: "intro"
      title: "初遇"
      on_enter: "state.npcs.suqingwan.affection >= 10"
      objectives:
        - "完成与苏清婉的第一次对话"
      narrator_hint: |
        苏清婉冷淡戒备，玩家主动开口。
        给出至少一个对话钩子。
      suggested_actions_on_enter:
        - type: "npc_emote"
          target: "suqingwan"
          emotion: "wary"
    
    - id: "branch_a"
      title: "追问真相"
      objectives:
        - "询问 NPC 过去"
      decisions:
        - when: "player.text matches /你过去|为什么/"
          next: "deep_dive"
        - when: "player.text matches /不问|不说/"
          next: "follow_silently"
        - default: "branch_a_stay"
      
    - id: "deep_dive"
      title: "深挖背景"
      pre_conditions:
        - "state.npcs.suqingwan.affection >= 40"
      narration_hint: "揭示 NPC 过去的一段重要经历"
  
  triggers:
    - type: "location"
      when: "state.world.location == '山门'"
      jump_to: "intro"
    - type: "time"
      when: "state.world.time_of_day == '子时'"
      jump_to: "night_event"
    - type: "event"
      when: "event.type == 'enemy_appear'"
      jump_to: "combat_1"
    - type: "threshold"
      when: "state.npcs.suqingwan.affection >= 90"
      jump_to: "love_confession"
```

### 4.2 触发器执行器

```python
# plugins/shared/rp/narrative_graph/executor.py

class NarrativeGraphExecutor:
    def __init__(self, graph):
        self.graph = graph
        self.current_node = graph['nodes'][0]  # 默认从 intro 开始
        self.history = []

    def evaluate_triggers(self, state, event_stream):
        """每回合结束后评估一次"""
        for t in self.graph.get('triggers', []):
            if self._eval_when(t['when'], state, event_stream):
                return self._jump_to(t['jump_to'])
        return None

    def _eval_when(self, expr, state, events):
        # 简化：用 asteval 或自写表达式求值器
        # 支持: state.X.Y.Z, AND, OR, NOT, ==, !=, <, >, <=, >=
        return safe_eval(expr, {'state': state, 'event': events[-1] if events else None})

    def evaluate_decisions(self, player_text):
        """玩家输入触发决策跳转"""
        for node in self.graph['nodes']:
            if node['id'] != self.current_node['id']:
                continue
            for d in node.get('decisions', []):
                if d.get('when') == 'default':
                    continue
                if self._match_decision(d, player_text):
                    return self._jump_to(d['next'])
            # 没匹配走 default
            for d in node.get('decisions', []):
                if d.get('when') == 'default':
                    return self._jump_to(d['next'])
        return None

    def _jump_to(self, node_id):
        new_node = next(n for n in self.graph['nodes'] if n['id'] == node_id)
        self.history.append((self.current_node['id'], node_id))
        self.current_node = new_node
        return new_node

    def is_pre_condition_met(self, node, state):
        return all(self._eval_when(c, state) in (True, 1) for c in node.get('pre_conditions', []))
```

### 4.3 特殊语法（Convai 兼容）

- `<speak>...</speak>` —— 强制 LLM 输出原文（不意译）
- `*` —— 强制立即跳到下一节点

**AgentOS 实现**：
- v1 兼容层：解析 prompt 中的 `<speak>` 标签，提取为 `narrative_graph.strict_lines`
- `*` 解析为决策的 `default` 跳转

---

## 5. 规则引擎（战斗 / 数值 / 事件）

### 5.1 战斗结算

```yaml
# 角色卡 engine.rules.combat
combat:
  formula:
    damage: "atk * (1 + uniform(-0.1, 0.1)) - def * 0.5"
    crit:   "uniform_random() < crit_rate"
    crit_mult: 2.0
  initiative:
    order: ["player", "npcs.suqingwan", "enemies.swordsman"]
    tiebreak: "dex"
  actions_per_turn: 1
  status_effects:
    poisoned:
      duration: 3
      on_tick: { path: "player.hp", op: "-", value: 5 }
```

```python
# plugins/shared/rp/combat/resolver.py

class CombatResolver:
    def __init__(self, rules, state_machine, rng):
        self.rules = rules
        self.state = state_machine
        self.rng = rng

    def resolve_round(self, actions):
        # 1. 校验 actions 合法性（用的是当前状态可用的技能/物品）
        validated = [self._validate(a) for a in actions]

        # 2. 按 initiative 排序
        ordered = self._sort_by_initiative(validated)

        # 3. 每个角色执行 action
        events = []
        for action in ordered:
            event = self._execute_action(action)
            events.append(event)

        # 4. 应用 status_effects
        for effect in self._active_effects():
            events.append(self._apply_effect(effect))

        # 5. 返回事件流（给 LLM 写故事的事实基础）
        return CombatRoundResult(
            events=events,
            state_changes=self._derive_state_changes(events),
            seed=self.rng.getstate(),
        )

    def _execute_action(self, action):
        if action['type'] == 'attack':
            atk = self.state.get(action['actor'], 'atk')
            tgt_def = self.state.get(action['target'], 'def')
            base = atk * (1 + self.rng.uniform(-0.1, 0.1))
            is_crit = self.rng.random() < self.rules['crit']
            damage = max(0, base - tgt_def * 0.5) * (self.rules['crit_mult'] if is_crit else 1)
            return {
                'actor': action['actor'],
                'target': action['target'],
                'action': 'attack',
                'damage': round(damage),
                'is_crit': is_crit,
                'hit': damage > 0,
            }
```

### 5.2 随机事件表

```yaml
# 角色卡 engine.rules.random_events
random_events:
  trigger_every_n_turns: 4
  pool:
    - id: "stranger_approach"
      weight: 0.20
      cooldown_rounds: 5
      narration_hint: "一个陌生人接近，眼神不善。"
      effects:
        - type: "spawn_event"
          event: "combat_intent"
    - id: "rain_starts"
      weight: 0.15
      narration_hint: "天色暗下来，豆大雨点落下。"
      effects:
        - type: "set_state"
          path: "world.weather"
          value: "雨"
    - id: "merchant_passby"
      weight: 0.30
      cooldown_rounds: 3
      narration_hint: "一个商人挑着担子经过。"
      effects:
        - type: "spawn_event"
          event: "merchant_offer"
```

```python
# plugins/shared/rp/rules/random_events.py

class RandomEventScheduler:
    def __init__(self, rules, rng):
        self.rules = rules
        self.rng = rng
        self.last_triggered = {}  # event_id -> round_count

    def maybe_trigger(self, current_round, state):
        if current_round % self.rules['trigger_every_n_turns'] != 0:
            return None
        eligible = []
        weights = []
        for ev in self.rules['pool']:
            cooldown = self.rules['pool'][0].get('cooldown_rounds', 0)
            since = current_round - self.last_triggered.get(ev['id'], -cooldown - 1)
            if since > cooldown:
                eligible.append(ev)
                weights.append(ev['weight'])
        if not eligible:
            return None
        chosen = self.rng.choices(eligible, weights=weights, k=1)[0]
        self.last_triggered[chosen['id']] = current_round
        return chosen
```

### 5.3 数值化设计要点

- 所有数值（伤害系数、暴击率、好感阈值、事件权重）走 `engine.rules` 配置
- 运营/创作者可改不改代码
- **确定性种子**：用 `Random(seed)` 让结果可复现（便于 QA 测试）

---

## 6. LLM 增强层（多模式 + 可调契约）

### 6.1 LLM 的角色定位（按模式分支）

> LLM 的「角色」完全取决于当前模式：

| 模式 | LLM 角色 | LLM 工作 | LLM 不工作 |
|---|---|---|---|
| **A. 纯 LLM** | 全权叙述者 + 状态自维护者 | 一切（含状态） | 无 |
| **B. 引擎 + LLM** | 故事生成器 | `narration` + 提议 | 规则判定、状态持久化 |
| **C. 纯引擎** | 不参与 | — | — |

> **模式 A 是 afengy 现状的等价物**——LLM 自己维护 HTML 状态栏、Mod、jailbreak 等。系统应当**等价支持**，而不是只把它视为"不完整"模式。
>
> **模式 C 等价于一个 CRPG**——LLM 完全不参与，确定性叙事、确定性分支、确定性战斗。

### 6.2 LLM 输入拼装（按模式分支）

模式 A、B、C 的 prompt 拼装规则不同：

```
┌──────────────────────────────────────────────────────────────────────┐
│ 模式 A（纯 LLM）—— 极简拼装                                            │
│ [1] SYSTEM: 角色卡 flavor.description + flavor.style_guide           │
│ [2] HISTORY: 全部历史对话 + 摘要                                       │
│ [3] PLAYER INPUT                                                       │
│ —— 不注入 state/lorebook/node，因为状态由 LLM 自维护                  │
│ —— Mod 由创作者在 flavor.system_prompt 里覆盖                          │
├──────────────────────────────────────────────────────────────────────┤
│ 模式 B（引擎 + LLM）—— 全量拼装                                         │
│ [1] SYSTEM: style_guide + actions 清单 + 全局规则                     │
│ [2] SCENE: 当前节点 hint + objectives                                  │
│ [4] LOREBOOK HITS                                                       │
│ [5] STATE SNAPSHOT                                                      │
│ [6] ACTION CHOICES（可选）                                            │
│ [7] HISTORY: 最近 N 轮 + L2 摘要                                       │
│ [8] PLAYER INPUT                                                       │
│ [9] EVENT CONTEXT（已落库的事实）                                      │
├──────────────────────────────────────────────────────────────────────┤
│ 模式 C（纯引擎）—— 无 LLM，跳过                                          │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.3 输出契约松紧档（Contract Levels）

**核心设计**：契约不是单一固定 Schema，而是**由创作者 / 玩家声明的档位**。

```yaml
# 角色卡顶部
character_card:
  engine:
    contract_level: 2     # 0~3 四档
```

| 档位 | 必含字段 | 可选字段 | 适用场景 |
|---|---|---|---|
| **0 — 纯文本** | `narration` (纯字符串) | — | 自由聊天 / 文学共创 / 情感陪伴 |
| **1 — 文本 + 选项** | `narration` | `choices[]` | 简单 RP（afengy 大多数作品） |
| **2 — 文本 + 选项 + 动作** | `narration` | `choices[]`, `actions[]` | 中度结构化 RP（带 BGM/立绘/动效） |
| **3 — 全结构化** | `narration`, `state_changes[]` | `choices[]`, `actions[]`, `thinking` | 引擎 + LLM 模式（最大约束） |

**关键点**：

- **契约宽松性 = 创造力空间**。档位 0 给最大叙事自由（LLM 完全自由发挥）；档位 3 给最大结构化（适合游戏机制）。
- **档位可动态切换** —— 同一角色卡可"这回合档位 0，下回合档位 3"，由系统根据玩家动作自动判断（玩家点击选项 → 档位 0；玩家输入开放动作 → 档位 3）。
- **档位越高，prompt 越长**（要教 LLM 输出结构）—— 但能换取更强的状态机集成。

### 6.4 输出契约 JSON Schema（按档位）

```python
# plugins/shared/rp/llm/contract_schemas.py

CONTRACT_SCHEMAS = {
    0: {  # 纯文本
        "type": "object",
        "required": ["narration"],
        "properties": {
                "narration": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
        },
        "additionalProperties": False,  # 严格——禁止额外字段
    },

    1: {  # +choices
        "type": "object",
        "required": ["narration"],
        "properties": {
                "narration": {"type": "string"},
                "choices": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "id":    {"type": "string"},
                            "text":  {"type": "string"},
                            "hint":  {"type": "string"},
                        },
                    },
                },
            },
        },
        "additionalProperties": True,  # 宽松——允许 LLM 多输出字段
    },

    2: {  # +actions
        "type": "object",
        "required": ["narration"],
        "properties": {
                "narration": {"type": "string"},
                "choices": {"type": "array"},
                "actions": {  # 仅可调用角色卡 engine.actions 中声明的 action
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {"type": {"type": "string"}, "params": {"type": "object"}},
                    },
                },
                "narration_meta": {
                    "type": "object",
                    "properties": {
                        "mood": {"type": "string"},
                        "pace": {"type": "string"},
                    },
                },
            },
        },
        "additionalProperties": True,
    },

    3: {  # 全结构化
        "type": "object",
        "required": ["narration", "state_changes"],
        "properties": {
                "narration": {"type": "string"},
                "choices": {"type": "array"},
                "actions": {"type": "array"},
                "state_changes": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "required": ["path", "op", "value"],
                        "properties": {
                            "path":   {"type": "string"},
                            "op":     {"enum": ["+", "-", "*", "/", "=", "set", "unset", "toggle", "append", "remove"]},
                            "value":  {},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "thinking": {"type": "string"},
                "narration_meta": {"type": "object"},
            },
        },
        "additionalProperties": True,
    },
}
```

### 6.5 「宽进严出」解析器（关键！）

> **关键设计**：契约失败的回退是**模式内降档**，不是**整体错误**。

```python
# plugins/shared/rp/llm/contract_resilient_parser.py

class ResilientContractParser:
    """尝试用最高档解析，失败就降档，最终兜底纯文本"""

    def parse(self, raw_text: str, target_level: int, declared_actions: list[str]) -> ParseResult:
        # 1. 提取 JSON（即使 LLM 输出混入自然语言也尝试）
        json_candidate = self._extract_json(raw_text)
        
        parsed = None
        used_level = None

        # 2. 从目标档往下试
        for level in range(target_level, -1, -1):
            schema = CONTRACT_SCHEMAS[level]
            try:
                validate(instance=json_candidate, schema=schema)
                parsed = json_candidate
                used_level = level
                break
            except (ValidationError, json.JSONDecodeError):
                continue

        # 3. 全部失败 → 整段文本当 narration（模式 A 等价）
        if parsed is None:
            return ParseResult(
                narration=raw_text.strip(),
                level=0,
                choices=[],
                actions=[],
                state_changes=[],
                fallback_reason='all_levels_failed',
            )

        # 4. Action 白名单校验（仅档位 2/3）
        if 'actions' in parsed:
            parsed['actions'] = [a for a in parsed['actions'] if a['type'] in declared_actions]

        # 5. State changes 校验（仅档位 3）
        if 'state_changes' in parsed:
            parsed['state_changes'] = self._filter_valid_changes(parsed['state_changes'])

        return ParseResult(
            narration=parsed['narration'],
            level=used_level,
            choices=parsed.get('choices', []),
            actions=parsed.get('actions', []),
            state_changes=parsed.get('state_changes', []),
            thinking=parsed.get('thinking'),
            narration_meta=parsed.get('narration_meta'),
        )

    def _extract_json(self, text: str):
        """从混杂文本中提取 JSON 对象（即使外层有散文）"""
        # 尝试直接 parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试找 ```json ... ``` 代码块
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试找首个 { 到末尾的 }（处理 LLM 在 JSON 后追加说明的情况）
        m = re.search(r'\{', text)
        if m:
            start = m.start()
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i+1])
                        except json.JSONDecodeError:
                            break

        raise json.JSONDecodeError('no JSON found', text, 0)
```

### 6.6 调用栈（按模式分支）

```python
# plugins/shared/rp/llm/narrator.py

class LLMNarrator:
    def __init__(self, llm_invoker, prompt_assembler, parser, action_executor, state_validator):
        self.llm = llm_invoker
        self.assembler = prompt_assembler
        self.parser = parser
        self.actions = action_executor
        self.validator = state_validator

    async def narrate(self, mode: str, contract_level: int, state, history, event_context, current_node):
        # 1. 按模式拼 prompt
        prompt = self.assembler.assemble(
            mode=mode,
            level=contract_level,
            state=state,
            history=history,
            event_context=event_context,
            current_node=current_node,
        )

        # 2. 调用 LLM（让 LLM 自由发挥）
        raw = await self.llm.invoke(prompt, stream=True)

        # 3. 宽进严出解析（关键：契约失败时回退宽松档）
        declared_actions = self.actions.list_ids()
        parsed = self.parser.parse(raw, target_level=contract_level, declared_actions=declared_actions)

        # 4. 按模式处理 state_changes
        if mode == 'engine_llm' and parsed.state_changes:
            validation = self.validator.validate(state, parsed.state_changes)
            accepted_changes = validation.accepted
            rejected = validation.rejected
        else:
            # 模式 A：LLM 提议的状态变更不被代码采纳，
            # 但 LLM 自己输出的 HTML 状态栏照样渲染（HTML 渲染器自己处理）
            accepted_changes = []
            rejected = []

        # 5. 执行 actions（不影响落库顺序）
        for action in parsed.actions:
            self.actions.execute(action, context={'state': state, 'node': current_node})

        return NarratorResult(
            narration=parsed.narration,
            level_used=parsed.level,           # 实际用的契约档（可能低于目标档）
            choices=parsed.choices,
            validated_changes=accepted_changes,
            rejected_changes=rejected,
            actions_executed=parsed.actions,
            fallback_reason=parsed.fallback_reason,
            thinking=parsed.thinking,
            narration_meta=parsed.narration_meta,
        )
```

### 6.7 流式输出策略

```python
async def narrate_streaming(self, ...):
    """流式推送 narration，但延迟落库直到契约解析完成"""

    # 1. 流式收 narration（玩家文本）
    raw_buf = ""
    async for token in self.llm.stream_invoke(prompt):
        raw_buf += token
        # 启发式：若 LLM 还没输出 {，说明是纯文本模式 A，直接流推送
        if '"narration"' not in raw_buf and '{' not in raw_buf:
            yield StreamChunk(text=token, kind='token', done=False)

    # 2. 完整文本拿到 → 解析契约
    parsed = self.parser.parse(raw_buf, target_level=contract_level, ...)

    # 3. 把解析后的 narration 标 is-done=True（前端拼装完成）
    yield StreamChunk(text=parsed.narration, kind='final', done=True,
                      choices=parsed.choices, level=parsed.level)
```

### 6.8 与"afengy 创作指南 §06 输出要求"对应

| afengy 创作指南原话 | AgentOS 实现 |
|---|---|
| "AI 实际收到的数据长什么样" | `agentos creator trace --level=2` 输出完整 prompt 拼装 |
| "权重优先级总结" | 模式 B 固定拼装顺序（§6.2）；模式 A 极简（仅 system + history） |
| "思维链" | 仅档位 3 可选 `thinking` 字段；模式 A 默认无（创作者可在 prompt 自加） |
| "Markdown/XML 格式" | 创作者在 prompt 里写；LLM 自由使用 |

---

## 7. Action 协议（代码执行 LLM 的指令）

### 7.1 Action 清单（角色卡声明）

```yaml
# 角色卡 engine.actions
actions:
  # 叙事类（LLM 输出后立即执行）
  - id: "npc_emote"
    description: "让 NPC 表现出某种情绪"
    params: { target: str, emotion: str }
    handler: "agentos.rp.ui:npc_emote"
    # 谁消费：前端 WebSocket 客户端
  
  - id: "play_sfx"
    description: "播放音效"
    params: { track: str, volume: float = 1.0 }
    handler: "agentos.rp.audio:play"
  
  - id: "bgm"
    description: "切换背景音乐"
    params: { track: str, loop: bool = true, fade_in_ms: int = 1500 }
    handler: "agentos.rp.audio:bgm"
  
  # UI 类
  - id: "show_choices"
    params: { options: list }
    handler: "agentos.rp.ui:show_choices"
  
  # 游戏类（影响下一回合）
  - id: "emit_event"
    params: { name: str, payload: dict }
    handler: "agentos.rp.events:emit"
  
  - id: "trigger_graph"
    params: { node: str }
    handler: "agentos.rp.narrative_graph:jump_to"
  
  - id: "spawn_loot"
    params: { items: list }
    handler: "agentos.rp.items:spawn"
```

### 7.2 Action 执行器

```python
# plugins/shared/rp/actions/executor.py

class ActionExecutor:
    def __init__(self, registry):
        self.registry = registry  # action_id -> handler
        self.audit = []

    def execute(self, action, context):
        handler = self.registry.get(action['type'])
        if not handler:
            self.audit.append({'type': 'unknown_action', 'action': action})
            return
        try:
            handler(action, context)
        except Exception as e:
            self.audit.append({'type': 'action_failed', 'action': action, 'err': str(e)})
            # 不抛——一个 action 失败不影响整个回合
```

### 7.3 与 afengy 的「快捷指令」按钮的关系

afengy 的【生成存档】【功法创建】【宗门创建】【法宝创建】等按钮，本质上是**预声明的 Action 模板**。AgentOS 用 `engine.actions` 显式声明它们 + 处理函数，比 afengy 的"prompt hack"清晰得多。

---

## 8. 记忆系统（复用 AgentOS 现有基础设施）

> **设计原则**：记忆**不重新发明**。AgentOS 0.2 内核已有 session / agent_config_load / 持久化引擎 / 上下文管理。RP 插件作为**消费者**，只做 RP 特有的"摘要+检索"附加值。

### 8.1 AgentOS 现有可复用设施

| 设施 | 位置 | RP 用法 |
|------|------|--------|
| **Session 内存** | 内核 session crate | 存储每回合 LLM 调用、玩家输入、玩家回复 |
| **agent_config_load** | 管道输入插件 `plugins/shared/pipeline/input/agent_config_load/` | 角色卡加载 = 注入 LLM 上下文 + 引擎配置 |
| **Kernel 存储** | `kernel/crates/db-admin` | 常规状态（HP/位置/物品/任务）落库 |
| **task.status 状态机** | 内核 task crate | 任务进度（pending / running / completed） |
| **tool-surface capability** | 内核 | 工具面过滤（角色卡声明的 actions 自动暴露给 LLM） |
| **多循环体 execution_context** | 内核 | agent_id 作为执行上下文键，与"装备"的工具/插件/状态同源 |
| **existing memory_palace**（afengy 等下游） | 第三方兼容 | 通过 adapter 接入，作为 L2 长期记忆的可选格式 |

### 8.2 三层记忆（与传统设计的对比）

```
┌─────────────────────────────────────────────────────────────────┐
│ AgentOS 内核已有                                                 │
│  - Session 内存：最近 N 轮 LLM 调用（input/output 元数据）       │
│  - Kernel 存储：常规状态（HP/位置/物品/任务进度）                 │
│  - agent_config_load：每次任务启动从磁盘读角色卡                  │
├─────────────────────────────────────────────────────────────────┤
│ RP 插件新增（仅做"附加值"）                                  │
│  - L1 摘要：把内核 session 的 N 轮调用压成 200 字（成本控制）    │
│  - L2 检索：跨会话时检索关键摘要（玩家档案 + 关键选择）          │
│  - lorebook 检索：按需加载设定条目（与 memory 解耦）             │
└─────────────────────────────────────────────────────────────────┘
```

**为什么不自己存？** 因为内核已经存了，**重用而不是增新**才符合 AgentOS 的"一切皆插件"原则 —— RP 插件是消费者而非提供者。

### 8.3 RP 插件需要做的"附加值"

```python
# plugins/shared/rp/memory/augmenter.py
# ——本类只做"摘要 + 检索"两件事，不持有原始数据

class RPMemoryAugmenter:
    """消费 AgentOS 内核的 session / storage，提供 RP 特有的 memory 增强"""

    def __init__(self, session_service, kernel_storage, llm, vector_store):
        self.session = session_service      # 来自 kernel session crate
        self.storage = kernel_storage       # 来自 kernel db-admin
        self.llm = llm
        self.vector = vector_store

    def summarize_session(self, task_id: str) -> str:
        """每 10 回合触发一次：把内核 session 中的 N 轮调用压成摘要"""
        # 1. 从 session 服务取最近 20 轮
        recent = self.session.get_messages(task_id, limit=20)
        # 2. 调用 LLM 生成摘要
        prompt = f"""基于以下对话，生成 200 字内的结构化摘要：
  
  - 关键选择与后果
  - 关系变化（NPC、阵营、玩家）
  - 剧情节点与未解之谜
  - 玩家档案更新
  
  对话：
  {self._format(recent)}"""
        summary = self.llm.invoke(prompt)
        # 3. 摘要存到内核 storage 的 metadata 字段（不是新建表）
        self.storage.append_task_metadata(task_id, key='summary', value=summary, ts=now())
        # 4. 同时写入向量库（用于跨会话检索）
        self.vector.add(text=summary, metadata={'task_id': task_id, 'kind': 'summary'})
        return summary

    def load_into_prompt(self, task_id: str, query: str = None, max_tokens: int = 800) -> str:
        """从内核 session + 摘要 + 向量库组成给 LLM 的 memory 段"""
        # 1. 最近 20 轮（直接从 session 服务读）
        recent = self.session.get_messages(task_id, limit=20)
        recent_text = self._format(recent)
        # 2. 早期对话的摘要（从内核 storage 的 metadata 读）
        summaries = self.storage.get_task_metadata(task_id, key='summary', limit=5)
        summary_text = '\n'.join(s.value for s in summaries)
        # 3. 跨会话相关性检索（仅在跨任务时使用）
        related = []
        if query:
            related_hits = self.vector.query(query, top_k=3, filter={'user_id': self.session.get_user_id(task_id)})
            related = [h.text for h in related_hits]
        # 4. 按 token 预算拼装
        return self._pack_within_budget(
            recent=recent_text,
            summaries=summary_text,
            related=related,
            max_tokens=max_tokens,
        )
```

### 8.4 与 afengy 「记忆区」对比

| 维度 | afengy 现状（LLM 自维护） | AgentOS（本设计） |
|---|---|---|
| 存储 | LLM 输出到 prompt 的 `<details>` 块 | **AgentOS 内核 session/storage 持久化** |
| Token 浪费 | 高（每回合全文塞入 prompt） | 低（按需加载摘要 + 检索） |
| 漂移风险 | 高（LLM 改了"事实"） | 零（事实由内核持久，摘要仅辅助） |
| 跨会话 | 难（仅靠 prompt 拼接） | 易（向量库检索） |
| 可调试 | 难（散落在对话中） | 易（结构化 metadata + summary） |
| 与 AgentOS 集成 | 无（外部系统） | **原生**（复用 session/storage） |

### 8.5 第三方记忆系统接入（如 memory_palace）

afengy 等下游使用的「memory_palace」是**自定义格式**（黑盒存储 LLM 状态）。AgentOS 通过 adapter 接入而非替换：

```python
# plugins/shared/rp/memory/adapters/memory_palace.py

class MemoryPalaceAdapter:
    """把 afengy 风格的 memory_palace 转换为 AgentOS session 调用"""

    def __init__(self, rp_memory):
        self.rp = rp_memory

    def import_from_palace(self, palace_data: dict, task_id: str):
        """从 afengy memory_palace 格式导入到 AgentOS session"""
        for entry in palace_data.get('items', []):
            self.rp.session.append_message(
                task_id=task_id,
                role='system',
                content=f"[imported from memory_palace] {entry['summary']}",
                metadata={'origin': 'memory_palace', 'id': entry.get('id')},
            )

    def export_to_palace(self, task_id: str) -> dict:
        """导出为 afengy 兼容格式（用于跨平台迁移）"""
        return {
            'format': 'memory_palace-v1',
            'items': [
                {
                    'id': m.metadata.get('id', str(uuid4())),
                    'summary': m.content,
                    'created_at': m.ts,
                }
                for m in self.rp.session.get_messages(task_id, kind='summary')
            ],
        }
```

### 8.6 记忆子系统边界（明确划定）

| 谁负责 | 不归谁 |
|---|---|
| AgentOS 内核：session 内存、task storage、agent_config_load | RP 插件不重写这些 |
| RP 插件：摘要生成、向量检索、跨会话关联 | 不自己存原始对话 |
| 创作者：声明「需要持久化的关键选择类型」 | LLM 不能改 kernel storage 中的常规状态 |
| LLM：仅生成摘要与建议标签 | 不能直接改 kernel 数据库 |

---

## 9. UI 与可视化

### 9.1 React 前端组件清单

| 组件 | 用途 |
|---|---|
| `<CharacterCard>` | 角色卡封面 + 标题 + Tag |
| `<ChatStream>` | 消息流（流式 narration + choices + actions） |
| `<StatusBar>` | 状态栏（HP/好感/位置/时间） |
| `<InventoryPanel>` | 物品栏 |
| `<QuestPanel>` | 任务追踪 |
| `<WorldMap>` | 世界地图 + 当前位置 |
| `<RelationshipGraph>` | 关系网络可视化 |
| `<NarrativeEditor>` | 叙事图编辑器（创作者端） |
| `<StateDebugger>` | 状态机调试器（创作者端） |
| `<LorebookEditor>` | 世界书条目编辑器 |
| `<CustomCSSSandbox>` | Custom CSS 沙箱渲染 |

### 9.2 状态条实时同步（关键）

```typescript
// 前端订阅后端 state stream
useEffect(() => {
  const ws = new WebSocket(`/api/task/${taskId}/state-stream`);
  ws.onmessage = (event) => {
    const change = JSON.parse(event.data);
    updateLocalState(change);  // 即时刷新，不等 LLM 流式输出
  };
  return () => ws.close();
}, [taskId]);
```

**为什么必须实时**：状态机是确定性事实，状态条更新不能等 LLM 写完叙述。

### 9.3 CustomCSS 沙箱

afengy 允许作者写自定义 CSS（详见创作指南 §08）。AgentOS 需要沙箱化：

```python
# plugins/shared/rp/ui/custom_css_sandbox.py

class CustomCSSSandbox:
    """仅允许 afengy 兼容的 #customized-XXX- 选择器"""

    BANNED_PATTERNS = [
        r'@import', r'@charset', r'expression\(',
        r'javascript:', r'behavior\s*:', r'-moz-binding',
    ]

    def validate(self, css: str) -> ValidationResult:
        # 1. 黑名单正则
        for pattern in self.BANNED_PATTERNS:
            if re.search(pattern, css, re.IGNORECASE):
                raise UnsafeCSS(f'banned pattern: {pattern}')
        # 2. 选择器白名单（afengy 风格）
        # 仅允许以 #customized- 开头的 ID 选择器 + 标准伪类
        ...
        # 3. 资源白名单（图片域名）
        ...
```

前端用 iframe 隔离渲染，作者 CSS 在沙箱中生效但不污染主界面。

---

## 10. AgentOS 插件蓝图

### 10.1 插件清单

按 AgentOS 架构公理「一切皆插件」，每个子系统 = 一个 Python 插件：

| 插件路径 | 角色 | 依赖 |
|---|---|---|
| `plugins/shared/rp/character_card/` | 角色卡加载、校验、导出 | - |
| `plugins/shared/rp/state_machine/` | 状态机引擎 + 契约校验 | character_card |
| `plugins/shared/rp/lorebook/` | 世界书匹配与注入 | character_card |
| `plugins/shared/rp/lorebook_rag/` | RAG 模式世界书（可选） | lorebook |
| `plugins/shared/rp/narrative_graph/` | 叙事图执行 | state_machine |
| `plugins/shared/rp/actions/` | Action 执行器 | state_machine |
| `plugins/shared/rp/combat/` | 战斗规则引擎 | state_machine |
| `plugins/shared/rp/rules/` | 通用规则引擎（随机事件等） | state_machine |
| `plugins/shared/rp/llm/narrator/` | LLM 叙事者 | 所有上述 |
| `plugins/shared/rp/memory/` | 三层记忆 | narrator |
| `plugins/shared/rp/ui/custom_css/` | CustomCSS 沙箱 | - |
| `plugins/shared/rp/creator_tools/` | CLI / lint / test / preview | 全部 |

### 10.2 与 AgentOS 公理的契合

- **「一切皆插件」**：每个子系统都是独立插件，符合架构公理
- **「能力即声明」**：`capabilities.tools` 注册角色卡操作 API
- **「契约冻结」**：JSON Schema 是稳定契约，插件边界不破坏

### 10.3 插件接口契约

```python
# plugins/shared/rp/state_machine/plugin.py

from agentos.plugin_sdk import Plugin, tool, capability

class StateMachinePlugin(Plugin):
    name = "agentos.rp.state_machine"

    @tool(
        name="rp.apply_state_change",
        description="应用一个已校验的状态变更",
        input_schema={...},
        output_schema={...},
    )
    def apply_state_change(self, task_id: str, change: dict) -> dict: ...

    @tool(
        name="rp.get_state_snapshot",
        description="获取任务当前状态快照（LLM 可读）",
    )
    def get_snapshot(self, task_id: str) -> dict: ...

    @capability("state-storage")
    def provide_state_storage(self):
        """声明本插件提供 state storage 能力"""
        return StateStorageBackend()
```

---

## 11. 三模式谱：可切换、可降级

### 11.1 重新定位：从「降级」到「模式谱」

之前的 §11 把「无 LLM」叫「降级模式」，**这是误导**——它不是降级，它是**第一类公民**。

> 「无 LLM」不是「LLM 失败后的兜底」，而是**一个独立可用的微型游戏类型**：玩家点选项推进剧情，narrative_graph 跳节点，状态机推进，纯确定性流程。它是「视觉小说」「选择剧情」「互动小说」——这些是 RPG 大品类中独立的一支。

「降级」只在**同一个角色卡内**有意义：玩家正在玩 LLM 模式，LLM API 挂了，自动切到同一角色卡的「无 LLM」分支（前提是角色卡有 narrative_graph + templates）。

### 11.2 三个最常使用的组合

#### A. 默认推荐：`core + with_llm`（首选，80% 创作者用这个）

```
LLM 写故事（narration + 模糊情感）
        ↓ 状态机永远决定
常规状态（HP/位置/时间/物品/基础战斗）
        ↓ 即便 LLM 挂掉
也能切到 B（用同一角色卡的 narrative_graph + templates）
```

**适用**：90% 的 RP 场景。需要玩家**轻量声明**（HP/MP/基础规则），不需要复杂的叙事图。

#### B. 离线/节省：`core + without_llm`（小型流程化游戏）

```
narrative_graph 跳节点（玩家选项 → 决策 → 下一节点）
        ↓ 模板拼接
node_template → 占位符替换 → 输出给玩家
        ↓ 状态机永远决定
所有状态（HP/位置/好感/物品）
```

**不是 CRPG**——是 visual novel / 文字冒险 / 选择剧情。够"小游戏"用，**不强求复杂度**。

**适用**：
- LLM API 挂了 / 超时 / 限流时的自动降级
- 玩家主动切"离线模式"（如飞行模式、节省 token）
- LLM 完全不可用的环境（局域网部署、企业内网）
- 创作者只想做一个简单的"剧情选择"小游戏，不想写 prompt

#### C. 完全规则化：`full + with_llm`（复杂 RP）

**适用**：复杂 RP（CRPG-like）+ LLM 美化叙事。Convai 风格。

#### D. 完全规则化无 LLM：`full + without_llm`（CRPG-lite）

**适用**：完整的 deterministic CRPG（无叙事变化，每次重玩相同）。

### 11.3 「无 LLM」的具体形态

不是抽象概念，是**具体的游戏类型**——参考已有视觉小说/文字冒险：

**最小可用特性集**：

```
1. 启动：玩家点「开始」
   ↓
2. 节点 intro：展示开场叙述
   ↓
3. 展示 3-4 个选项
   ↓
4. 玩家点选项
   ↓
5. narrative_graph 跳转到目标节点
   ↓
6. 状态机更新（HP -5 if 走危险路径，+5 if 走安全路径）
   ↓
7. 节点展示一段模板化叙述
   ↓
8. 回到第 3 步（循环）
```

**示例：苏清婉剑冢奇缘（无 LLM 版）**

```yaml
# character_card.yaml —— 仅核心字段
character_card:
  spec: "agentos-character-card-v1"
  
  engine:
    llm: "without_llm"              # 离线模式
    rule_complexity: "core"
    
    variables:
      player:
        hp: { type: int, default: 100, min: 0, max: 100 }
      world:
        location: { type: enum, values: [剑冢, 集市, 客栈] }
        time_of_day: { type: enum, values: [子时..亥时], default: 申时 }
  
  # 核心规则
  combat:
    formula: "atk * (1 + rand(-0.1, 0.1)) - def * 0.5"
  
  # 叙事图（小型流程化）
  narrative_graph:
    nodes:
      - id: intro
        template: |
          暮色四合，你在剑冢洞口遇到白衣女子苏清婉。
          她的眼神冷如寒冰：「你来这里做什么？」
          当前 HP: {{ state.player.hp }} | 位置: {{ state.world.location }}
        choices:
          - { text: "恭敬回答：「路过此地」", next: polite }
          - { text: "沉默不语",              next: silent }
          - { text: "反问：「你是谁？」",   next: question }
      
      - id: polite
        template: "苏清婉微微皱眉，但语气稍缓：「既然如此，请便。」"
        effects:
          - { path: "npcs.suqingwan.affection", op: "+", value: 3 }
        choices:
          - { text: "继续赶路", next: leave }
          - { text: "问她是否需要帮助", next: help }
      
      - id: silent
        template: "苏清婉上下打量你一眼，转身离开。"
        effects:
          - { path: "npcs.suqingwan.affection", op: "+", value: 1 }
        next: leave
      
      - id: question
        template: "苏清婉冷笑：「问人名字之前，先报上自己的。」"
        choices:
          - { text: "自报姓名",   next: self_intro }
          - { text: "继续沉默",   next: silent }
      
      - id: self_intro
        template: "苏清婉听完后点点头：「某乃苏清婉，这剑冢是我的地方。」"
        effects:
          - { path: "npcs.suqingwan.affection", op: "+", value: 5 }
        next: help
      
      - id: help
        template: |
          苏清婉微微叹息：「我在寻一柄失落此地的剑。
          你若能助我，我必重谢。」
          当前好感: {{ state.npcs.suqingwan.affection }}/100
        choices:
          - { text: "答应帮她", next: quest_accept }
          - { text: "婉拒",     next: leave }
      
      - id: quest_accept
        template: "（开启支线任务：在剑冢中寻找失落之剑）"
        effects:
          - { path: "quests.lost_sword.status", op: "=", value: "active" }
          - { path: "world.location",           op: "=", value: "剑冢" }
        next: leave
      
      - id: leave
        template: |
          你们分道扬镳。江湖路远，后会有期。
          --- 故事未完待续 ---
          最终 HP: {{ state.player.hp }} | 最终好感: {{ state.npcs.suqingwan.affection }}
        ending: true
```

**这就是「无 LLM」的完整形态**——一段可独立发布的、有意义的小游戏。**够小才真实**。

### 11.4 模板 Pack（与 narrative_graph 配合）

```yaml
# 角色卡 flavor.template_pack
templates:
  default:
    location_label: "位置"
    time_label:     "时辰"
    hp_label:       "气血"
    status_template: |
      <div class="status-bar">
        <span>{{ state.player.hp }}/100 气血</span>
        <span>{{ state.world.location }}</span>
        {% if state.npcs.suqingwan %}
        <span>好感 {{ state.npcs.suqingwan.affection }}/100</span>
        {% endif %}
      </div>
```

### 11.5 LLM 健康度检测与自动切换

```python
class LLMHealthMonitor:
    """监测 LLM API 健康度，触发自动切换"""

    def __init__(self, window=20):
        self.window = window
        self.recent_results = deque(maxlen=window)  # 最近 N 次结果

    def record(self, success: bool, latency_ms: int):
        self.recent_results.append({'success': success, 'latency': latency_ms, 'ts': now()})

    def is_available(self) -> bool:
        """连续失败 3 次或成功率 < 50% 视为不可用"""
        if len(self.recent_results) < 3:
            return True
        recent_3 = list(self.recent_results)[-3:]
        if not all(r['success'] for r in recent_3):
            return False
        success_rate = sum(1 for r in self.recent_results if r['success']) / len(self.recent_results)
        return success_rate >= 0.5

    def avg_latency_ms(self) -> float:
        if not self.recent_results:
            return 0
        return sum(r['latency'] for r in self.recent_results) / len(self.recent_results)


class ModeAutoSwitcher:
    """根据健康度自动在轴上切换"""

    def __init__(self, character_card, llm_health, mode_with_llm, mode_without_llm):
        self.card = character_card
        self.health = llm_health
        self.mode_with = mode_with_llm
        self.mode_without = mode_without_llm

    def current_mode(self):
        declared = self.card.engine.llm
        if declared == 'with_llm':
            if self.health.is_available():
                return self.mode_with
            else:
                # LLM 不可用，但角色卡声明需要 LLM —— 切到 without_llm（如果可能）
                if self.card.engine.narrative_graph:
                    return self.mode_without
                # 没有 narrative_graph —— 没法降级
                raise LLMUnavailableError('no fallback for this character card')
        return self.mode_with  # declared 'without_llm'

    def narrate(self, ...):
        mode = self.current_mode()
        return mode.narrate(...)
```

### 11.6 「无 LLM」 vs 「CRPG」 的边界

> **重要：不要把「无 LLM」做得过重。**
>
> 「无 LLM」是**轻量级**的小型流程化游戏，玩家点选项，节点跳转，状态机推进。够用就行。
>
> 如果创作者想做真正的 CRPG（复杂战斗系统、复杂 AI、复杂事件），**需要 LLM**。让规则复杂度进入 `full` + with_llm 模式（用 LLM 处理 NPC 对话、事件描述、剧情推进）。
>
> **架构上的取舍**：无 LLM 模式不追求"完整游戏体验"，只追求"游戏还能玩"。这是 LLM 不可用时的兜底，不是 fail-over 到一个完整 RPG 的追求。

### 11.7 与 AgentOS 内核的协同

模式切换与 AgentOS 内核的契约：

```python
# 内核 task crate 暴露的模式声明
@dataclass
class TaskConfig:
    mode: Literal['with_llm', 'without_llm']
    rule_complexity: Literal['none', 'core', 'full']
    # 内核根据此决定：
    #   - 是否调用 LLM invoker
    #   - 是否启用状态机
    #   - 是否执行 lorebook 检索
    # RP 插件作为 consumer 接收这些参数
```

**内核不需要知道 RP 细节**——它只看到 with_llm / without_llm、none/core/full 两个轴。RP 插件解释这两个轴的具体含义（启用哪些子系统）。

---

## 12. 与 afengy.com 创作指南逐条对照

> **关键变化**：之前的设计文档试图把 afengy 当成「不完整」模式去覆盖——这是错的。**正确的对照是：afengy 的每一条建议都可以在二轴框架的某个 cell 上找到对应实现，但**不会强行套用到所有 cell**。

### 12.1 对照表（按 afengy 创作指南章节）

| afengy 创作指南原话 | afengy 现状（LLM-only） | AgentOS 实现（按 cell 分流） |
|---|---|---|
| **Vol.01 §02 基础篇**：前后置词 + 提示词是核心 | ✓ 三段 prompt 拼装 | `core + with_llm`：拆为 system + lorebook + state + history；`none + with_llm`：等价 afengy（仍支持） |
| "提示词是写给 AI 看的工作手册" | ✓ 整段 | `core+`：拆分为「常规状态」由代码持有 + 「模糊状态」由 LLM 自由发挥；`none+`：LLM 全权 |
| **Vol.01 §03 Markdown/格式**：消除重复、结构化 | ✓ Markdown 提示 | `agentos creator lint --check=duplicates` 自动检测 |
| **Vol.01 §03 玩法设计**：好感度、随机事件、分支 | ✓ 提示词硬写 | `core+`：`engine.variables` + `engine.rules.random_events`；`full+`：`engine.narrative_graph` |
| **Vol.01 §03 思维链** | ✓ LLM 自加 | `narrator.thinking` 字段（档位 3） |
| **Vol.02 §01 上下文是什么**：主提示词 + 前后置词 + 世界书 + 历史 = token | ✓ 拼装 | `agentos creator trace` 显示完整拼装；按 cell 自动裁剪 |
| **Vol.02 §02-03 世界观 / 角色设定** | ✓ Markdown 段落 | `flavor.description`（文学性） + `engine.variables`（状态性） 分离 |
| **Vol.02 §04 输出要求**：文风、思维链、约束 | ✓ 全在 prompt | `core+`：LLM 自由（`flavor.style_guide`）；`full+`：state_changes 走 schema |
| **Vol.02 §05 状态栏与记忆区**：状态栏 + 记忆区 | ✓ LLM 自维护 + HTML | `core+`：`ui.status_bar_template` + 模板渲染（HTML 沙箱）；`full+`：状态机 + 摘要 |
| **Vol.02 §06 组装**：排列顺序 | ✓ 作者凭经验 | `core+`：固定拼装顺序（`system → scene → lorebook → state → history → event`）；`none+`：极简（system + history） |
| **Vol.02 §07 世界书进阶**：插入顺序 + 扫描深度 | ✓ 配置项 | `lorebook.position` + `sql.scan_depth`，与 afengy 同义 |
| **Vol.02 §08 UI 与美化**：HTML/CSS | ✓ 作者手写 | `core+` + `CustomCSSSandbox`（iframe + CSP + 白名单） |
| **Vol.03 §04 阶段性总结**：平台规范 + 内容分级 | ✓ 创作者守则 | 沿用 AgentOS 的"公序良俗"框架；不照搬 RTA 等 |

### 12.2 哪些是 AgentOS 真正新增的？

| afengy 没有 / 弱 | AgentOS 新增 |
|---|---|
| ❌ 状态机 / 数值校验 | `engine.variables` schema + State Contract Validator |
| ❌ 规则引擎（战斗/事件/掉落） | `engine.rules` 配置 + `agentos.rp.combat` |
| ❌ 叙事图（节点 + 触发器） | `engine.narrative_graph` + 可视化编辑器 |
| ❌ Action 协议 | `engine.actions` 显式声明 + 客户端执行 |
| ❌ 离线小型流程游戏 | `core + without_llm` 第一类公民（visual novel 级） |
| ❌ LLM 健康度检测 + 自动切换 | `LLMHealthMonitor` + `ModeAutoSwitcher` |
| ❌ 跨会话记忆 | `RPMemoryAugmenter`（摘要 + 向量检索，复用内核） |
| ❌ 契约松紧可调 | `contract_level` 4 档（0/1/2/3） |
| ⚠️ 「好感度」「位置」「时间」靠 LLM 维护 | `engine.variables` + 代码校验（核心 cell 强制） |

### 12.3 哪些是 afengy 强而 AgentOS 暂不强？

| afengy 强 | AgentOS 暂不强 | 影响 |
|---|---|---|
| 海量现成作品（创作指南 + 大量 UGC） | 无 | 启动期需要靠创作者迁移 |
| 积分/付费/月卡/礼包体系 | 无 | 商业化路径需独立设计 |
| 多端 App / APK / iOS / Windows | 仅 Web | 装机版路径需独立设计 |
| 论坛 / 社交矩阵 | 无 | 社区建设需独立设计 |
| 破甲生态（Mod/jailbreak） | ❌ 不做 | 合规优先；LLM 用其他 LLM（合理） |

### 12.4 与 afengy 的关系

**不是替代，是共存 + 迁移**：
- afengy 用户量大、内容多——他们的 Mod/作品应当能被 AgentOS 读
- afengy 模式 (`none + with_llm`) 是 AgentOS 的合法 cell —— 用户想用 Mod 想用破甲，AgentOS 也支持
- 但 AgentOS 的核心 cell (`core + with_llm`) 比 afengy 更稳定 —— HP/位置/时间不会漂移

**迁移工具（M4 实施）**：
- `agentos rp import afengy-character` —— 解析 afengy 作品页面 + 提示词
- 自动转换：Markdown 提示词 → flavor.description；HTML 状态栏模板 → ui.status_bar_template；好感度规则 → engine.variables
- memory_palace 数据 → Adapter 导入 AgentOS 内核 session

---

按"**酒馆兼容 → 默认推荐模式 → 离线 → 复杂 → 生态迁移**"5 个阶段。

### Phase 0（M0，2 周）：SillyTavern 资产兼容（地基）

> **M0 是后续一切的根基。** 不做完 M0，M1+ 用户无法从 ST 迁移，所有"扩展市场"都是空谈。
>
> 目标：让 SillyTavern 用户的角色卡、STscript、Regex、Lorebook、Slash Commands 100% 在 AgentOS 中跑。

- `agentos.rp.character_card`：YAML 角色卡 + V2/V3 PNG 双向导入导出（PNG tEXt chunk 读写）
- `agentos.rp.st_compat`：ST 内部 API stub 适配层（~30+ 个核心 API）
- `agentos.rp.stscript_runtime`：STscript 解释器（支持 `/set`, `/let`, `/add`, `/trigger`, `/sys`, `/run`, `/?` 等）
- `agentos.rp.lorebook`：完整 ST World Info 兼容（关键词、优先级、递归扫描、常驻/蓝灯/灰灯）
- `agentos.rp.regex`：ST Regex Scripts 兼容执行
- `agentos.rp.extension_loader`：ST Extension 通过 compat 层加载
- 创作者工具：
  - `agentos rp import <file.png>` —— V2/V3 PNG → AgentOS YAML
  - `agentos rp export <card.yaml> --format png-v3` —— 反向导出
  - `agentos rp upgrade <card.yaml>` —— 推断 schema + 建议增强
- 前端：ST 风格 `<ChatStream>`、`<StatusBar>`、`<ChoicesPanel>`、`<QuickReplyBar>`
- **里程碑**：ST 用户"导入即用"，体验与 ST 100% 一致
- **关键不变量**：导入的角色卡**可重新导出回 ST**，不丢失数据

### Phase 1（M1，3 周）：核心模式（`core + with_llm`）—— 80% 用户

> 目标：让 80% 的创作者能用 —— 只需声明 HP/MP/位置/时间等基础规则 + 一段 prompt。

- `agentos.rp.character_card` 增强：YAML 角色卡（含 `engine.llm`、`engine.rule_complexity`、`engine.variables`）
- `agentos.rp.state_machine`：仅常规状态 schema + 校验（HP/MP/位置/时间/物品）
- `agentos.rp.lorebook` 增强：5 种触发模式（keyword + threshold + scene + quest + regex）
- `agentos.rp.llm.narrator`：档位 0-2 输出契约（纯文本/文本+choices/文本+actions），档位 3 暂缓
- `agentos.rp.actions`：基础 action 注册与执行
- 前端：`<ChatStream>`、`<StatusBar>`、`<CustomCSSSandbox>`（基础）
- 创作者工具：`agentos creator new/lint/preview`
- **差异化点（与 afengy 对比）**：HP/位置/时间等常规状态由代码持有——即便 LLM "忘了"也不会漂移
- **差异化点（与 ST 对比）**：schema 化变量（无需 STscript 维护）、状态校验、模板渲染

### Phase 2（M2，2 周）：离线小型流程游戏（`core + without_llm`）—— LLM 不可用时

> 目标：玩家点选项推进剧情，narrative_graph 跳节点，状态机推进 —— 一个"视觉小说"级别的体验。

- `agentos.rp.narrative_graph`：节点 + 决策 + effects + 触发器（轻量版，不做编辑器）
- `agentos.rp.llm.template_narrator`：模板叙事器（jinja2 / 类似）
- `agentos.rp.template_pack`：模板渲染（角色卡 `flavor.template_pack`）
- `agentos.rp.mode_auto_switch`：LLM 健康度检测 + 模式自动切换
- `agentos.rp.memory.augmenter`：摘要 + 检索（**复用 AgentOS 内核 session/storage**，不建新表）
- 前端：`<NarrativeGraphRunner>`（轻量版）、`<ChoicesPanel>`
- 创作者工具：`agentos creator export-visual-novel`
- **差异化点**：把"无 LLM"做成第一类公民（视觉小说级别），不只当降级兜底

### Phase 3（M3，3 周）：复杂 RP（`full + with_llm`）—— 高级创作者

> 目标：Convai 风格，复杂叙事图 + 完整 Action 协议 + 完整契约档位 3（含 `state_changes` schema 提议）。

- `agentos.rp.narrative_graph`：完整版（含编辑器、触发器可视化）
- `agentos.rp.llm.narrator`：档位 3（state_changes 提议 + 校验落库）
- `agentos.rp.combat`：战斗规则引擎（确定性）
- `agentos.rp.rules`：随机事件 / 掉落 / 好感判定
- `agentos.rp.lorebook_rag`：嵌入检索（可选启用）
- `agentos.rp.action_executor`：完整 action 注册表（前端可执行）
- 前端：`<NarrativeEditor>`、`<RelationshipGraph>`、`<CombatUI>`、`<CustomCSSSandboxAdvanced>`
- 创作者工具：`agentos creator test`（模拟器）

### Phase 4（M4，2 周）：生态与迁移

> 目标：让现有 RP 资产（afengy / character.ai）能迁入；发布 AgentOS 原生作品。

- `agentos.rp.adapter.afengy`：afengy memory_palace 适配器
- `agentos.rp.adapter.character_ai`：character.ai 资产转换
- `agentos creator market`：发布到市场
- 前端：`<Marketplace>`、`<ImportWizard>`

### 实施优先级总结

| 阶段 | 内容 | 用户群 | 优先级 |
|---|---|---|---|
| **M0** | **SillyTavern 兼容** | **ST 用户（最大潜在群体）** | ★★★★★ |
| M1 | `core + with_llm` | 80% 创作者 | ★★★★★ |
| M2 | `core + without_llm` | 离线 / 节省 / 视觉小说爱好者 | ★★★★ |
| M3 | `full + with_llm` | 复杂 RP 创作者 | ★★★ |
| M4 | `full + without_llm` + 生态迁移 | 高级 + 老用户 | ★★ |

**关键**：**M0 不做完不开始 M1** —— 没有 ST 兼容，后续所有"扩展市场"都是空谈。

### M0 实施细节（M0 的最详细计划）

```
M0.1 (3 天) PNG 解析器
  - 用 Pillow 或自定义 PNG 解码器读 tEXt chunk
  - 解析 V2 JSON / V3 JSON（带扩展字段）
  - 输出 AgentOS 内部结构

M0.2 (3 天) ST 内部 API stub 清单
  - 列出 ST 内部 API（getContext, setExtensionPrompt, eventOn, etc）
  - 实现 stub（80% 用默认值，其余 not-implemented）
  - 写兼容性测试（用 ST 文档对照）

M0.3 (3 天) STscript 解释器
  - 解析 /cmd 命令
  - 映射到 AgentOS 状态变更
  - 支持变量、流程控制、触发器

M0.4 (2 天) Lorebook 兼容
  - 完整支持 ST World Info 字段
  - 关键词、优先级、递归扫描

M0.5 (2 天) Regex 兼容执行
  - 与 ST Regex Script 引擎一致
  - 输出可重新导出

M0.6 (2 天) Extension 加载器
  - QuickJS 运行时
  - ST API stub 注入
  - 基础 extension 测试用例

M0.7 (1 天) PNG 写出器
  - 写 V2/V3 JSON 到 tEXt chunk
  - PNG 字节完整往返（无数据丢失）
```

### 用户路径示例：ST → AgentOS → 增强

```
Week 0：ST 用户有 100 个角色卡（PNG）
Week 1：用户安装 AgentOS
         运行 agentos rp import *.png
         → 100 个 YAML 角色卡
Week 2：用户选择 5 个最爱角色，运行 agentos rp upgrade
         → 自动推断 schema + 建议增强
         → 用户接受 3 个角色的 schema 增强
Week 3：用户在新角色卡上写 YAML 角色卡
         → 享受：状态机 schema、叙事图、模式谱
Week 4：用户导出回 ST
         → ST 用户社区看到"AAA-增强版"
         → 新用户开始尝试 AgentOS
```

---

## 14. ADR 决策清单

按 AGENTS.md「ADR 制度」，下列决策必须写 ADR（存到 `docs/decisions/`）：

1. **二轴模式框架** —— LLM 可用性 × 规则复杂度；为什么用二轴而非三模式枚举？[已写：`2026-10-06-rp-engine-first-architecture.md`]
2. **角色卡 spec 选型** —— YAML vs JSON vs TOML？为什么？
3. **状态机契约格式** —— JSON Schema vs Protobuf vs Pydantic？为什么？
4. **常规状态 vs 模糊状态边界** —— 谁决定哪些字段归哪边？
5. **世界书触发策略** —— 关键词 vs 嵌入 vs 混合？RAG 何时启用？
6. **Action 协议** —— 是否有平台特定的扩展点？
7. **持久化策略** —— per-task_id 还是 per-character_id？跨会话边界？
8. **记忆复用策略** —— 如何对接 AgentOS 内核 session/storage？
9. **CustomCSS 沙箱** —— iframe vs CSP vs Worker 哪种？
10. **角色卡导出格式** —— 哪些支持？与 SillyTavern 的兼容边界
11. **LLM 健康度检测** —— 健康度如何定义？切换延迟？
12. **叙事图 schema** —— 与 Convai Narrative Design 的兼容策略
13. **状态变更审计** —— 留存多久？合规要求？

---

## 15. 测试与质量保障

### 15.1 测试矩阵

| 类型 | 工具 | 检查内容 |
|---|---|---|
| **单元测试** | pytest | 状态机、规则引擎、触发器纯函数 |
| **契约测试** | pytest + jsonschema | LLM 输出满足 JSON Schema |
| **降级测试** | pytest | 无 LLM 时也能跑通完整回合 |
| **模拟测试** | `agentos creator test` | 用规则化脚本模拟 5 轮对话 |
| **反漂移测试** | pytest | 100 轮后状态不变量 |
| **性能测试** | locust | LLM 降级 / 高并发 / 大 lorebook |

### 15.2 反漂移测试（核心）

```python
# tests/rp/test_state_consistency.py
def test_state_invariants_after_100_rounds():
    """100 回合模拟后，状态必须满足 schema 不变量"""
    sm = StateMachine(task_id="t1", schema=SAMPLE_SCHEMA)
    simulator = RoundSimulator(seed=42)

    for _ in range(100):
        # 玩家输入 → 解析 → 规则引擎 → LLM (mock) → 校验 → 落库
        llm_output = simulator.simulate_llm_output(sm.state)
        validation = validator.validate(sm.state, llm_output['proposed_state_changes'])
        sm.apply_validated({'accepted': validation.accepted, 'rejected': validation.rejected})

    # 不变量 1：HP 在 schema 范围
    assert 0 <= sm.state['player']['hp'] <= 100
    # 不变量 2：好感在 schema 范围
    for npc in sm.state['npcs'].values():
        assert 0 <= npc['affection'] <= 100
    # 不变量 3：物品栏数值非负
    for item in sm.state['player'].get('inventory', []):
        assert item['qty'] >= 0
    # 不变量 4：物品未超过上限
    assert len(sm.state['player'].get('inventory', [])) <= 50
    # 不变量 5：flags 字典只包含 schema 声明的 key
    assert set(sm.state['world'].get('flags', {}).keys()) <= {'saw_secret', 'met_old_zhou', ...}
```

### 15.3 降级模式测试

```python
def test_degraded_mode_runs_without_llm():
    """无 LLM 时，跑通 10 回合"""
    sm = StateMachine(task_id="t1", schema=SAMPLE_SCHEMA)
    template_narrator = TemplateNarrator(SAMPLE_TEMPLATE_PACK)
    rules_engine = RulesEngine(SAMPLE_RULES, sm)

    for round in range(10):
        # 玩家输入（固定脚本）
        player_action = SCRIPT[round]
        event = rules_engine.resolve(player_action)
        sm.apply_validated(event['state_changes'])

        # 模板叙事（不调用 LLM）
        result = template_narrator.narrate(sm.state, [], event, current_node)
        assert result.narration  # 有叙述输出
        assert len(result.suggested_choices) >= 2  # 至少 2 个选项

    # 状态推进正常
    assert sm.state['world']['time_of_day'] != '申时'  # 时间已推进
```

### 15.4 与 AGENTS.md 测试准则契合

- **断行为不断实现**：测不变量，不测内部 mock
- **防拟合**：用真实 LLM 跑至少 1 条路径；删值实验（改疑似硬编码常量测试必须变红）
- **关键路径走真实依赖**：LLM 调用不能全 mock，至少有 e2e 测试跑真实 DeepSeek

---

## 16. 总结

本设计文档**把"角色扮演模式"从"LLM-first"重新定位为"Engine-first"**。

核心立场：

1. **游戏机是本体** —— 没有 LLM 也能玩（用模板叙事降级）
2. **状态是事实，叙述是表达** —— 状态由代码 DB 持有
3. **LLM 永远只是「故事生成器」** —— 不参与规则判定、不持有数值权威
4. **一次 LLM 调用 = 一个事实增量** —— 输出结构化契约，前置后置由 code 负责

差异化：

| vs. 目标 | 我们赢在哪 |
|---|---|
| **afengy.com / character.ai / 筑梦岛** | 真正的状态机、规则引擎、叙事图、可视化、跨会话记忆、降级模式、开放生态 |
| **SillyTavern / RisuAI** | 状态由代码强制（非作者自己写脚本维护）；结构化输出契约；可视化编辑器 |
| **Convai / Inworld** | RP 而非游戏 NPC；中文市场；SillyTavern V2/V3 兼容 |

实施路径：4 个 Phase、12 周、12+ 插件。

需要我继续：
1. 逐插件写**实现规格**（每个 Python 插件的 module/class/method 详细定义）？
2. 把 1 个插件写成**代码骨架 + 一个测试**？
3. 写 ADR 草案（上面 12 个 ADR 选 1-2 个）？