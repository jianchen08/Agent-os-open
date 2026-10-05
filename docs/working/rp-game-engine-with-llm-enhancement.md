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

> **核心架构立场**：本系统首先是一台**游戏机**（game engine），其次才是一个**故事生成器**（narrative generator）。
>
> 把 LLM 当成游戏机的"美术 / 配音 / 编剧"，而不是当主角。状态、规则、逻辑、确定性流程全部由代码承载，LLM 只负责把这些事实翻译成自然语言、生成情绪色彩、提供选项灵感。
>
> **对比市面上常见路线**（afengy.com / character.ai / JanitorAI / SpicyChat / 筑梦岛等）：它们都是 **LLM-first** —— 先有聊天，再让玩家自己用「好感度 + 状态栏 + Mod」缝缝补补。本设计是 **Engine-first** —— 先有完整的状态机、规则引擎、叙事图，再让 LLM 来给这辆车装内饰。
>
> **直接对标**：Convai（叙事图 + Agentic Actions + State of Mind）、Inworld（Goals + Actions + Memories + Knowledge Graph）、Hidden Door（世界结构 + 多 NPC）、Charisma.ai（脚本场景 + AI 混合）、SillyTavern 的 Variables + STscript（作为"工具侧"参考）。

---

## 目录

0. [设计哲学与架构原则](#0-设计哲学与架构原则)
1. [角色卡标准（Character Card）](#1-角色卡标准character-card)
2. [游戏引擎核心：状态机子系统](#2-游戏引擎核心状态机子系统)
3. [世界书 / Lorebook 子系统](#3-世界书--lorebook-子系统)
4. [叙事图（Narrative Graph）](#4-叙事图narrative-graph)
5. [规则引擎（战斗 / 数值 / 事件）](#5-规则引擎战斗--数值--事件)
6. [LLM 增强层（不是基础）](#6-llm-增强层不是基础)
7. [Action 协议（代码执行 LLM 的指令）](#7-action-协议代码执行-llm-的指令)
8. [记忆系统（三层架构）](#8-记忆系统三层架构)
9. [UI 与可视化](#9-ui-与可视化)
10. [AgentOS 插件蓝图](#10-agentos-插件蓝图)
11. [降级模式：LLM 不可用时也能跑](#11-降级模式llm-不可用时也能跑)
12. [与 afengy.com 创作指南逐条对照](#12-与-afengycom-创作指南逐条对照)
13. [实施路线](#13-实施路线)
14. [ADR 决策清单](#14-adr-决策清单)
15. [测试与质量保障](#15-测试与质量保障)

---

## 0. 设计哲学与架构原则

### 0.1 四个核心公理

1. **游戏机是本体** —— 没有 LLM，系统也要能跑（用模板叙事）；有 LLM，系统跑得更好。
2. **状态是事实，叙述是表达** —— HP / 好感 / 位置 / 任务进度是「事实」，必须在数据库；LLM 只能读事实、提议修改、由代码校验落库。
3. **LLM 永远只是「故事生成器」** —— 不参与规则判定、不持有数值权威、不替代逻辑门。LLM 的输出必须是结构化契约（JSON Schema），其中 `narration` 字段才是给玩家看的。
4. **一次 LLM 调用 = 一个事实增量** —— 不是"聊一整段"。一次 LLM 调用只产出：①`narration`（故事文本）②`proposed_state_changes`（提议改动）③`suggested_actions`（建议动作）④`suggested_choices`（建议选项）。其它都是代码层的事。

### 0.2 与「LLM-first」路线对比

| 维度 | LLM-first（afengy 等） | Engine-first（本设计） |
|---|---|---|
| 状态归属 | 让 LLM 自由发挥，状态栏 HTML 由 LLM 自己输出 | 状态由代码 DB 持有，LLM 无权直接改 |
| 数值来源 | 提示词写"好感度 0-100，请更新" | `variables_schema` 声明，代码校验 |
| 战斗 | 用 prompt + 思维链硬写 | 战斗由规则引擎跑，LLM 只写叙事 |
| 分支 | 用 prompt 模拟状态机 | 真正的状态图（节点 + 触发器） |
| 重玩性 | 每次都不同，依赖运气 | 由确定性分支 + 可变叙事组合 |
| 反作弊 | 靠 jailbreak / Mod 防御 | 代码层硬校验 |
| Token 消耗 | 全部塞 prompt | 世界书按需检索 + 状态摘要 |

### 0.3 术语表

| 术语 | 含义 |
|---|---|
| **Engine Tick** | 引擎时钟的一"回合"：玩家输入 → 解析 → 规则引擎 → LLM 叙事 → Action 执行 → 状态落库 |
| **State Machine** | 状态机：确定性数值状态 |
| **Schema** | 状态变量声明（类型、范围、tag） |
| **Narrative Graph** | 叙事图：节点（目标）+ 边（决策）+ 触发器 |
| **Trigger** | 触发器：位置/时间/事件/阈值 → 切换节点 |
| **Action** | 动作：LLM 输出中可由客户端执行的原子操作 |
| **Narrator** | 叙事者：LLM 的角色身份，仅产出文本与提议 |
| **Rules Engine** | 规则引擎：战斗/随机/事件/掉落/好感判定等纯逻辑模块 |
| **Degraded Mode** | 降级模式：LLM 不可用时，用模板拼接叙事 |

---

## 1. 角色卡标准（Character Card）

### 1.1 角色卡哲学

角色卡不是"一段 prompt"，而是一个**游戏规则包**：
- 它声明这个世界有哪些变量（HP、位置、好感…）
- 它声明这个世界有哪些动作（移动、攻击、对话…）
- 它声明这个世界有哪些 NPC、剧情节点、触发器
- 它声明 LLM 的"语气与风格"约束

LLM 看到这张卡，理解的是"我是一名旁白，要为这个游戏的当前状态生成一段文字"。

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

## 6. LLM 增强层（不是基础）

### 6.1 LLM 的角色定位

> **LLM 是「故事生成器」，不是「游戏机」。**
>
> 它的工作：
> 1. 把状态机快照翻译成「玩家可读的故事」
> 2. 提议「下一回合可能的选项」
> 3. 提议「NPC 的情绪反应」
>
> 它不工作：
> 1. 计算伤害（那是规则引擎的事）
> 2. 判定合法性（那是代码校验的事）
> 3. 持久化状态（那是数据库的事）
> 4. 触发事件（那是事件调度器的事）

### 6.2 LLM 输入拼装（明确顺序）

```
┌─────────────────────────────────────────────────────────┐
│ [1] SYSTEM PROMPT                                        │
│     - 角色卡的 flavor.style_guide                        │
│     - 角色卡的 engine.actions 的清单（仅供 LLM 知道）    │
│     - 全局规则（禁用违禁内容、输出格式等）               │
│                                                          │
│ [2] SCENE INJECTION（按叙事图）                          │
│     - 当前节点：nodes                                    │
│     - 节点 hint：节点.narrator_hint                  │
│     - 节点 objectives：节点.objectives              │
│                                                          │
│ [3] LOREBOOK HITS（按世界书）                            │
│     - 关键词触发条目（按 position 插入）                 │
│     - 场景/任务/阈值触发条目                             │
│                                                          │
│ [4] STATE SNAPSHOT（实时状态）                           │
│     - 玩家 HP/MP/物品                                   │
│     - NPC 好感/位置/情绪                                │
│     - 世界时间/天气                                      │
│                                                          │
│ [5] ACTION CHOICES（可选）                              │
│     - 玩家本回合可执行的动作清单                         │
│                                                          │
│ [6] HISTORY（历史对话 + 摘要）                          │
│     - 最近 N 轮原始对话                                   │
│     - 早期对话的 LLM 摘要                                │
│                                                          │
│ [7] PLAYER INPUT                                         │
│     - 玩家本回合输入                                     │
│                                                          │
│ [8] EVENT CONTEXT（本回合发生的事实）                    │
│     - 战斗事件流                                        │
│     - 随机事件 ID                                        │
│     - 状态变更记录（已落库的事实）                       │
└─────────────────────────────────────────────────────────┘
```

### 6.3 LLM 输出契约

```python
# plugins/shared/rp/llm/narrator_contract.py

NARRATOR_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["narration", "proposed_state_changes"],
    "properties": {
        "narration": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
        },
        "narration_meta": {
            "type": "object",
            "properties": {
                "mood": {"enum": ["neutral", "tense", "sad", "happy", "angry"]},
                "pace":  {"enum": ["slow", "normal", "fast"]},
            },
        },
        "proposed_state_changes": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["path", "op", "value", "reason"],
                "properties": {
                    "path":   {"type": "string"},
                    "op":     {"enum": ["+", "-", "*", "/", "=", "set", "unset", "toggle", "append", "remove"]},
                    "value":  {},
                    "reason": {"type": "string"},
                },
            },
        },
        "suggested_actions":  {"type": "array"},
        "suggested_choices":  {"type": "array"},
        "thinking": {"type": "string"},
        "_metadata": {"type": "object"},
    },
}
```

### 6.4 LLM 调用栈

```python
# plugins/shared/rp/llm/narrator.py

class LLMNarrator:
    def __init__(self, llm_invoker, prompt_assembler, validator, action_executor):
        self.llm = llm_invoker
        self.assembler = prompt_assembler
        self.validator = validator
        self.actions = action_executor

    async def narrate(self, state, history, event_context, current_node):
        # 1. 拼 prompt
        prompt = self.assembler.assemble(
            state=state,
            history=history,
            event_context=event_context,
            current_node=current_node,
        )

        # 2. 调用 LLM
        response = await self.llm.invoke(
            prompt,
            response_format=NARRATOR_OUTPUT_SCHEMA,
            stream=True,
        )

        # 3. 解析 + 校验
        parsed = parse_json(response)
        validation = self.validator.validate(state, parsed['proposed_state_changes'])

        # 4. 执行 actions（不影响落库顺序）
        for action in parsed.get('suggested_actions', []):
            self.actions.execute(action, context={'state': state, 'node': current_node})

        return NarratorResult(
            narration=parsed['narration'],
            narration_meta=parsed.get('narration_meta'),
            validated_changes=validation.accepted,
            rejected_changes=validation.rejected,
            suggested_choices=parsed.get('suggested_choices', []),
        )
```

### 6.5 流式输出

LLM 的 `narration` 字段是唯一面向玩家的内容，应该流式推送：

```python
async def narrate_streaming(self, ...):
    prompt = self.assembler.assemble(...)
    async for chunk in self.llm.stream_invoke(prompt, response_format=NARRATOR_OUTPUT_SCHEMA):
        # LLM 在流式输出 JSON 时，逐 token 出来
        # 我们需要：等 JSON 完整闭合，再执行 action
        # 但 narration 部分可以提前推送到前端
        if chunk.json_complete:
            yield NarrationChunk(text=chunk.narration_so_far, done=True)
            return
```

或者用 structured outputs（OpenAI/Anthropic 的 grammar-constrained decoding）一次性拿到完整 JSON，再异步推流。

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

## 8. 记忆系统（三层架构）

### 8.1 三层结构

| 层 | 范围 | 存储 | 触发时机 |
|---|---|---|---|
| **L0 短期** | 最近 N 轮对话 | 内存 / Redis | 每回合直接进 prompt |
| **L1 中期** | 当前会话的摘要 | SQLite | 每 5-10 轮由 LLM 二次摘要 |
| **L2 长期** | 跨会话的玩家档案 | 向量数据库 + 关系表 | 跨会话 / 关键时刻 |

### 8.2 记忆子系统的实现

```python
# plugins/shared/rp/memory/system.py

class MemorySystem:
    def __init__(self, llm, store, vector_store):
        self.llm = llm
        self.store = store                # SQLite
        self.vector_store = vector_store  # 本地嵌入 + 检索

    def load_short_term(self, task_id, limit=20):
        return self.store.get_messages(task_id, limit=limit)

    def summarize_to_long_term(self, task_id):
        """每 10 回合触发一次"""
        recent = self.store.get_messages(task_id, limit=20)
        prompt = f"""基于以下对话，生成 200 字内的结构化摘要：
  
  - 关键选择与后果
  - 关系变化（NPC、阵营、玩家）
  - 剧情节点与未解之谜
  - 玩家档案更新
  
  对话：
  {self._format(recent)}"""
        summary = self.llm.invoke(prompt)
        self.store.append_summary(task_id, summary)
        # 同时写入向量库
        self.vector_store.add(
            id=f'{task_id}:summary:{now()}',
            text=summary,
            metadata={'task_id': task_id, 'type': 'summary'},
        )

    def retrieve_long_term(self, query, task_id, top_k=3):
        """玩家输入 → 检索相关历史摘要"""
        emb = self.vector_store.embed(query)
        results = self.vector_store.query(emb, top_k=top_k, filter={'task_id': task_id})
        return [r['text'] for r in results]
```

### 8.3 与 afengy 「记忆区」对比

afengy 让作者用 `<details><summary>记忆区</summary>...</details>` 让 LLM 自己维护记忆（在 prompt 里）：
- ❌ 高 token 浪费
- ❌ 易漂移（LLM 改了前面的"事实"）
- ❌ 不可调试

本设计的优势：
- ✅ 记忆由代码 + LLM 摘要共同管理，结构化
- ✅ 关键选择进入 L2 长期，可跨会话
- ✅ 摘要可被检索

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

## 11. 降级模式：LLM 不可用时也能跑

### 11.1 设计原则

游戏机本体（状态机 + 规则引擎 + 叙事图 + Action）在没有 LLM 时**必须能跑**。这时用**模板叙事**代替 LLM 生成。

### 11.2 降级模式实现

```python
class NarratorEngine:
    """叙事引擎——抽象基类"""

    def narrate(self, state, history, event_context, current_node) -> NarratorResult: ...


class LLMNarrator(NarratorEngine):
    """正常模式：调用 LLM"""
    ...


class TemplateNarrator(NarratorEngine):
    """降级模式：用模板拼接"""

    def __init__(self, template_pack):
        self.template_pack = template_pack  # 角色卡 flavor.template_pack

    def narrate(self, state, history, event_context, current_node):
        # 1. 选择模板
        template = self._choose_template(current_node, event_context)

        # 2. 填占位符
        narration = template.format(
            player_name=state.player.get('name', '你'),
            npc_name=event_context.get('npc_name', '她'),
            location=state.world.location,
            time_of_day=state.world.time_of_day,
            event=event_context.get('summary', ''),
            ...
        )

        # 3. 从模板中提取可选的 state_changes / actions（声明式）
        declared = self._extract_declared_effects(template)

        return NarratorResult(
            narration=narration,
            narration_meta={'mood': 'neutral', 'pace': 'normal'},
            validated_changes=declared.get('state_changes', []),
            suggested_actions=declared.get('actions', []),
            suggested_choices=declared.get('choices', []),
        )
```

### 11.3 模板 Pack

```yaml
# 角色卡 flavor.template_pack
templates:
  intro:
    text: "{time_of_day}时分，{player_name}走进{location}。{npc_name}正{action}。"
    variables:
        - name: action
          choices: ["独自发呆", "低头研读古籍", "擦拭手中的剑"]
  combat_win:
    text: "{player_name}的剑招凌厉，{enemy}应声倒地。"
  combat_lose:
    text: "{enemy}的攻击命中，{player_name}踉跄后退。"
```

### 11.4 自动降级触发

```python
class NarratorWithFallback:
    def __init__(self, llm_narrator, template_narrator, llm_health):
        self.llm = llm_narrator
        self.template = template_narrator
        self.health = llm_health

    def narrate(self, ...):
        if self.health.is_available() and self.health.success_rate() > 0.9:
            try:
                return self.llm.narrate(...)
            except LLMAvailabilityError:
                pass
        return self.template.narrate(...)
```

**降级模式价值**：当 LLM API 不可用 / 超时 / 限流时，游戏不停摆——玩家仍能玩，只是叙事变成模板化。这对稳定性和玩家体验是关键差异化。

---

## 12. 与 afengy.com 创作指南逐条对照

| afengy 创作指南原话 | 现状（LLM-only） | AgentOS 实现 |
|---|---|---|
| "提示词是写给 AI 看的工作手册" | ✓ 一段文字，全给 LLM | ✗ 改成"游戏规则包"，LLM 只看其中一部分 |
| "消除重复表述" | ✓ 作者手动精简 | `agentos creator lint --check=duplicates` 自动检测 |
| "善用结构化格式 (Markdown/XML)" | ✓ 用 Markdown 增强 LLM 阅读 | ✓ 强制 YAML / JSON Schema，YAML 段落填空模板 |
| "好感度系统" | ✓ 提示词写"好感度 0-100" | `engine.variables.npcs.X.affection` 强 schema |
| "随机事件" | ✓ 提示词写"3-5 回合后触发" | `engine.rules.random_events.pool` + 调度器 |
| "分支与记忆" | ✓ 提示词写"关键选择记录" | `engine.narrative_graph` + `memory_system` |
| "思维链" | ✓ LLM 自己 <thinking> | `narrator.thinking` 字段，前后端分离渲染 |
| "正向指令优于否定指令" | ✗ 容易写成"不要 X" | ✓ 代码层强制：negative instruction lint 警告 + 规则引擎拒绝 |
| "Token 消耗" | ✓ 作者手动估算 | `agentos creator lint --token-estimate` 自动 |
| "世界书按需加载" | ✓ 关键词触发 | ✓ + 场景触发 + 任务触发 + 阈值触发 + RAG |
| "状态栏 HTML 模板" | ✓ LLM 自由输出 HTML | ✓ `ui.status_bar_template` + 自动从 state 渲染 |
| "HTML/CSS 美化" | ✓ 作者写 CSS | ✓ + 沙箱（iframe + CSP + 白名单） |
| "玩家/NPC 信息栏" | ✓ LLM 自己维护 | ✓ `engine.variables` 多 group |
| "记忆区维护" | ✓ LLM 在 prompt 里维护 | ✓ 代码 + LLM 摘要协作 |
| "AI 实际收到的数据长什么样" | ✓ 创作者看不见 | ✓ `agentos creator trace` 显示完整拼装 |
| "权重优先级总结" | ✓ 作者凭经验 | ✓ `system → scene → lorebook → state → history → event` 代码固定 |
| "快捷指令按钮" | ✓ 字符串模板提示词 | ✓ `engine.actions` 显式声明 + handler |
| "破甲词" | ✓ 作者字符串拼接 | ✗ 不做（合规优先） |
| "HTML 详细介绍" | ✓ 作者写 HTML | ✓ `flavor.description_long` HTML + 沙箱渲染 |

---

## 13. 实施路线

按"差异化最大化 + 借鉴 Convai"双轴，分 4 个阶段：

### Phase 1（M1，2 周）：角色卡 + 状态机基础

- `agentos.rp.character_card`：YAML/JSON + V2/V3 PNG 导入导出
- `agentos.rp.state_machine`：schema 化状态 + 契约校验
- `agentos creator lint`：静态检查
- 前端：`<ChatStream>`、`<StatusBar>`
- **差异化点**：状态由代码持有（afengy 缺失）

### Phase 2（M2，3 周）：世界书 + 记忆

- `agentos.rp.lorebook`：5 种触发模式
- `agentos.rp.lorebook_rag`：嵌入检索（可选）
- `agentos.rp.memory`：三层记忆
- 前端：`<LorebookEditor>`、`<MemoryInspector>`
- **差异化点**：RAG + 跨会话记忆

### Phase 3（M3，3 周）：叙事图 + Action

- `agentos.rp.narrative_graph`：节点编辑器 + 触发器
- `agentos.rp.actions`：结构化输出 + WebSocket
- 前端：`<NarrativeEditor>`、`<RelationshipGraph>`
- **差异化点**：可视化叙事图（afengy 无）

### Phase 4（M4，4 周）：规则引擎 + 战斗 + 降级模式

- `agentos.rp.combat`：战斗结算 + 数值化
- `agentos.rp.rules`：随机事件 / 掉落 / 好感判定
- `agentos.rp.llm.narrator` 与 `agentos.rp.llm.template_narrator`：双模式
- `agentos creator test`：模拟器测试
- 前端：`<Marketplace>`、`<CreatorLinter>`
- **差异化点**：降级模式（LLM 不可用时也能玩）

---

## 14. ADR 决策清单

按 AGENTS.md「ADR 制度」，下列决策必须写 ADR（存到 `docs/decisions/`）：

1. **角色卡 spec 选型** —— YAML vs JSON vs TOML？为什么？
2. **状态机契约格式** —— JSON Schema vs Protobuf vs Pydantic？为什么？
3. **状态变更来源边界** —— LLM 提议 vs 代码计算 vs 玩家操作的具体边界
4. **世界书触发策略** —— 关键词 vs 嵌入 vs 混合？RAG 何时启用？
5. **Action 协议** —— 是否有平台特定的扩展点？
6. **持久化策略** —— per-task_id 还是 per-character_id？跨会话边界？
7. **跨会话记忆边界** —— 哪些属于 L2 长期？需要 PII 过滤吗？
8. **CustomCSS 沙箱** —— iframe vs CSP vs Worker 哪种？
9. **角色卡导出格式** —— 哪些支持？与 SillyTavern 的兼容边界
10. **降级模式触发条件** —— LLM 健康度如何定义？切换延迟？
11. **叙事图 schema** —— 与 Convai Narrative Design 的兼容策略
12. **状态变更审计** —— 留存多久？合规要求？

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