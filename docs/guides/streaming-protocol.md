# 流式事件协议（Streaming Protocol）

> 面向**想给灵汐 AgentOS 0.2 提供实时流式能力**的插件开发者，以及维护消息链路的前端/内核工程师。
> 本文定义流式事件的**平台公共契约**：所有消息实时通道（LLM 流式回复、插件实时进度/结构化卡片）发射的事件信封统一走本协议。
> 单一真值源：`config/kernel_capabilities/streaming.json`（内核入口校验 + 前端消费 + 插件发射端均读本文件，不读代码副本）。
> 决策背景见 [ADR 2026-08-22 流式链路重写为平台公共契约](decisions/2026-08-22-streaming-protocol-rewrite.md)。
> **协议状态：已采纳（文档先行）**——本文描述的是目标契约；实施进度（前端模块/网关校验/后端字段）以 ADR「实施顺序」为准，未全部落地前插件暂不可实际接入（接线前置见 §5）。

---

## 目录

- [1. 总览与设计原则](#1-总览与设计原则)
- [2. 事件信封通用字段](#2-事件信封通用字段)
- [3. 事件类型与载荷](#3-事件类型与载荷)
- [4. 消息生命周期（前端状态机）](#4-消息生命周期前端状态机)
- [5. 插件接入指南](#5-插件接入指南)
- [6. 持久化语义（displayed vs persisted）](#6-持久化语义displayed-vs-persisted)
- [7. 对账与认领](#7-对账与认领)
- [8. part 类型注册表（渲染扩展点）](#8-part-类型注册表渲染扩展点)
- [9. 测试与门禁](#9-测试与门禁)
- [附录 A：LLM 路径事件序列示例](#附录-allm-路径事件序列示例)

---

## 1. 总览与设计原则

流式事件协议是 0.2 的**平台公共能力**（与内核能力契约 chat/db_admin/metrics 同构）：
不是 LLM 路径的私有内部约定。任何插件声明 `capabilities.streaming` 后，按本协议
发射事件即可把实时内容推送到前端消息区，无需改动内核与前端代码。

设计原则（对齐业界成熟范式——ChatGPT/Claude 前端、Vercel AI SDK useChat 流式协议）：

| 原则 | 含义 |
|------|------|
| 精确寻址 | 每个事件携带 `(pipeline_id, message_id)`；前端只做 `update(pipelineId, messageId, delta)`，**禁止全局搜索/位置猜测** |
| 单一消息数组 | 乐观 user、流式中 assistant、插件展示消息全在同一个数组，靠 `status` 状态机区分生命周期 |
| 认领而非驱逐 | 用户消息确认后按 `client_message_id` 认领（id 迁移 + 补权威 seq），**永不消失** |
| 对账合并收敛 | 刷新/重连后与远端按 id/cmid 合并，不整表替换、不丢弃乐观/流式中消息 |
| 顺序只由权威 seq 裁决 | 已确认消息按后端 sequence 排序；未确认消息按 timestamp（发送在前） |
| 契约 fail-closed | 非法事件（缺必选字段/形态不符）由内核网关丢弃 + 告警，不静默放行 |

---

## 2. 事件信封通用字段

所有事件共用的信封字段（事件类型专属字段见 §3）：

| 字段 | 必选 | 形态 | 说明 |
|------|------|------|------|
| `pipeline_id` | 是 | 非空字符串 | 管道坐标（前端消息路由键）。LLM 路径=引擎 route_id；插件路径=插件已注册管道 id |
| `message_id` | 是 | 见下表 | 本条消息唯一标识（精确寻址键）。**id 空间按前缀隔离**（见下） |
| `_threadId` | 否 | `thread-` 前缀 | 会话坐标（归属与断线补漏定位）。**仅 `stream_start` 定义**（消息级坐标，随占位建立） |
| `sequence` | 否 | 整数 | 事件序号（进程内单调递增，仅调试定位，**非消息权威 seq**）。**仅 `stream_chunk` 定义**；注意 `new_message.sequence` 是另一个语义（消息权威 seq），`stream_end` 用 `final_sequence` |
| `persist` | 否 | 布尔 | 持久化语义：`true`=正式消息（落库，刷新保留）；缺省/`false`=纯展示（只进 store，刷新弃）。**缺省统一 `false` 不分路径**——内核 LLM 路径发射时显式携带 `true` |

### message_id 命名空间（前缀隔离，防冲突）

后端与前端共享同一个消息 id 空间，**冲突会发生**——因此协议强制前缀隔离
（单一真值源在 `config/kernel_capabilities/streaming.json` 的
`x-message-id-namespaces`，结构化清单：`prefix`/`owner`/`plugin_forbidden`/
`pattern`，内核网关与前端按同一清单校验）：

| 前缀 | 归属 | pattern | 说明 |
|------|------|---------|------|
| `a_` | 内核 LLM 路径（保留） | `^a_[0-9a-f]{32}$` | ws_session dispatch 生成 `a_`+uuid.simple()；引擎落库优先作 record_id。**插件禁止使用**（冒用会把插件 chunk 打进 LLM 占位气泡） |
| `mc_` | 引擎内容指纹（保留） | `^mc_[0-9a-f]{64}$` | `compute_message_id` 规范化 SHA256（user 消息 record_id 走此空间）。**插件禁止使用**（与远端真实消息 id 重叠 → 对账误合并/覆盖历史） |
| `p_` | **插件强制命名空间** | `^p_[0-9a-z_-]{1,63}$` | 插件发射的 `message_id` 必须匹配本 pattern，**不与 a_/mc_ 结构性重叠**，对账永不误合并 |
| （无前缀）裸 uuid | 前端乐观 user 消息（保留） | uuid v4 带连字符 | 发送瞬间的临时 id（=client_message_id），不在后端 id 空间；**认领时 UI id 保持不变**，后端权威 record_id（`mc_` 指纹）记入独立 `recordId` 字段（见 §4/§7）。**插件禁止使用裸 id**（与乐观 user 撞车） |

校验执行：内核 event-bus 网关按清单 fail-closed——插件事件 `message_id` 不匹配
`p_` 命名空间 pattern → 丢弃 + 告警；前端收到 `a_`/`mc_`/裸 id 前缀的事件时按
事件来源信任（LLM 路径由内核签发），不额外拦截。**id 冲突由前缀隔离结构性
杜绝，对账/寻址不再猜**。

---

## 3. 事件类型与载荷

> 本表是人读速览；**机器读真值源是 `config/kernel_capabilities/streaming.json`**
> （含完整 JSON Schema、必选/形态校验），两端实现与机械闸都消费它。

| 事件 | 必选字段 | 载荷语义 | 前端操作 |
|------|---------|---------|---------|
| `stream_start` | pipeline_id, message_id | 一条新消息开始流式输出 | 按 (pipeline_id, message_id) 建占位消息（status='streaming'） |
| `stream_chunk` | pipeline_id, message_id, content | 正文增量文本 | 追加到目标消息的 text part |
| `thinking_start` | pipeline_id, message_id | 思考开始 | push 新 thinking part（state='streaming'） |
| `thinking_chunk` | pipeline_id, message_id, content | 思考增量文本 | 追加到目标消息的 thinking part |
| `thinking_end` | pipeline_id, message_id | 思考结束（可带 duration_ms） | thinking part state='done' |
| `tool_start` | pipeline_id, message_id, call_id | 工具调用开始（name/args） | push tool_call part（state='calling'） |
| `tool_result` | pipeline_id, message_id, call_id | 工具结果（result/error/success/result_data） | 按 call_id 更新 tool_call part（state='done'/'error'） |
| `new_message` | pipeline_id, message_id | 消息确认（权威终态）：assistant 完整形态 + user_message 权威版 + client_message_id | ① 按 cmid 认领乐观 user（UI id 不变，权威 id/seq 记入 recordId）；② assistant parts 权威合并收尾 |
| `stream_end` | pipeline_id, message_id | 成功终止（final_sequence/parts/full_content 兜底） | 收尾 + 同步权威 seq |
| `stream_error` | pipeline_id, message_id | 失败终止（error） | 标记 status='failed' + error（不删除、不合成气泡） |

事件时序（典型序列，**非强制**——前端对乱序宽容，见下）：

```
stream_start → [thinking_start → thinking_chunk* → thinking_end]*
              → [tool_start → tool_result]*
              → stream_chunk*          ← text 与 thinking/tool 可交替多轮
              → 收尾
```

收尾语义：`new_message`（权威确认）与 `stream_end`（兜底收尾）**可先后都到达**
（new_message 先确认、stream_end 后到只补 final_sequence，幂等）；`stream_error`
与成功路径互斥。

前端对**乱序/缺失事件是宽容的**：chunk 先于 start 到达 → 自动建占位；
thinking_end 丢失 → 超时兜底置 done；new_message 缺失 → stream_end 兜底收尾；
全部缺失 → 对账/刷新补正。前端**不宽容**的是寻址失效（事件乱序错挂到别的消息）。

---

## 4. 消息生命周期（前端状态机）

```text
            发送瞬间                      确认（new_message）
 user:  ── sending ────────────────→ completed
             │                            ▲
             │ 90s 超时                  │ 按 client_message_id 认领
             ▼                            │
           failed（可重试，复用 cmid 幂等重发）

             stream_start              new_message / stream_end
 assistant: ── streaming ────────────▶ completed
             │
             └────────── stream_error ─▶ failed（保留已输出内容 + error 标记）
```

状态转移规则：

- **sending → completed**：唯一路径是 `new_message` 事件携带同 `client_message_id`
  的 `user_message` 权威版 → 认领（权威 record_id/sequence 记入独立字段
  `recordId`，**UI 寻址 id 保持前端 uuid 不变**、补权威 sequence）。
- **streaming → completed**：`new_message`（权威 parts 合并）或 `stream_end`
  （final_sequence 同步 + parts 兜底合并）。
- **任意状态 → failed**：`stream_error` 或发送 90s 超时。失败消息**保留内容**，
  显示重试入口；重试复用同一 cmid（后端幂等键保证不重复落库）。

---

## 5. 插件接入指南

接入三步：

### 步骤 1：manifest 声明 `capabilities.streaming`

```json
// plugins/shared/<category>/<name>/plugin.json
{
  "id": "my_streamer",
  "capabilities": {
    "tools": [],
    "services": [],
    "route_signals": [],
    "lifecycle_hooks": ["on_load", "on_unload"],
    "streaming": {
      "events": ["stream_start", "stream_chunk", "thinking_start", "thinking_chunk", "thinking_end", "stream_end"],
      "part_types": ["progress_card"],
      "persist": false
    }
  }
}
```

- `events`（可选，缺省=全部事件类型均可发射）：声明插件实际发射的事件类型，
  供校验器（G2）检查声明与实现一致性。
- `part_types`（可选）：插件自定义渲染的 part 类型（见 §8）。
- `persist`（可选，缺省 false）：插件事件的默认持久化语义。

> **接线前置（实施时落地）**：`capabilities.streaming` 与既有四键
> （tools/services/route_signals/lifecycle_hooks）平级，但 Rust 侧
> `ManifestCapabilities`（kernel/crates/core/src/traits.rs:1127）带
> `#[serde(deny_unknown_fields)]`——**不先加字段，任何插件声明 `streaming` 都会被
> 严格反序列化拒绝**（与当年 `capabilities.resources` 同款教训）。实施顺序见 ADR：
> 先扩展 ManifestCapabilities + 前端 pluginDeclarationValidate + G2 校验器，
> 再放行声明。

### 步骤 2：通过 event-bus 发射事件

```python
# 插件代码内（任何时机）
event_bus = self.get_capability("event-bus")
await event_bus.notify("emit", {
    "event": "stream_start",
    "payload": {
        "pipeline_id": "9c8e051a...",   # 已注册管道（前端 registerPipeline 或活跃管道）
        "message_id": "p_my_progress_001",  # ★ 必须以 p_ 开头（插件强制命名空间，防与 a_/mc_ 冲突）
        "persist": False,
    },
})
# ... 后续 stream_chunk / thinking_* / tool_* / stream_end 同信封（message_id 一致）
```

### 步骤 3：前端渲染

- 消息自动出现在目标管道消息列表（流式占位 → 增量内容 → 收尾）。
- 自定义 part 类型需在前端注册渲染组件（§8）；内置类型（text/thinking/tool_call）
  开箱即用。

---

## 6. 持久化语义（displayed vs persisted）

| `persist` | 语义 | 生命周期 |
|-----------|------|---------|
| `true`（内核 LLM 路径显式携带） | 正式消息（引擎落库） | 刷新/重连后由 API 对账保留 |
| `false`/缺省（统一缺省，插件路径典型用法） | 纯展示（实时进度、临时状态） | 只进前端 store，刷新即弃，不进对账 |

区分原因：插件实时进度（如"正在处理第 3/10 步"）不应污染历史记录；
正式消息（如 AI 回复、任务结果）必须持久化。对账逻辑只认 `persist=true` 面。

---

## 7. 对账与认领

对账（刷新/重连/断线补漏）统一按 **id / client_message_id 双向合并收敛**（业界双键对账范式，Telegram client_msg_id + 权威 message_id 同构）：

| 本地状态 | 远端返回 | 合并结果 |
|---------|---------|---------|
| 无 | 有 | 插入（按权威 seq 排序） |
| sending（乐观 user，id=cmid） | 有（同 cmid） | **认领**：权威 record_id/sequence 记入独立字段（`recordId`），**UI 寻址 id 保持前端 uuid 不变**，status='completed' |
| streaming | 有 | 远端权威字段覆盖，本地流式内容保留（合并收尾） |
| completed | 有 | 远端覆盖（同步后端最终状态） |
| 任意 | 无 | 保留（乐观/流式中，等确认或超时） |

**认领（echo）替代驱逐（evict）+ UI 寻址 id 永不迁移**是本协议的两条核心：

- **认领**：乐观用户消息确认后**升级**为权威消息，而不是删除后等待 API 补数——"发送后用户消息消失"结构性不可能发生；
- **id 稳定**：`id` 字段（UI 寻址/React key）在消息整个生命周期不变；后端权威 id 记入独立 `recordId` 字段（业界双字段范式：UI 坐标与持久化主键各司其职，不互相覆盖）。对账按 (id ∪ clientMessageId) 双键匹配。

---

## 8. part 类型注册表（渲染扩展点）

流式事件里的 part 渲染按类型分发（与工具卡片的 render 声明同一机制）：

```ts
// 前端注册自定义 part 渲染组件（插件声明 part_types 后由插件包注册）
// 名字必须与 manifest 声明的 part_types 一致（本例 = 'progress_card'）
registerPartRenderer('progress_card', ProgressCard)
```

内置 part 类型（开箱即用）：`thinking`（思考折叠卡片）、`text`（正文）、
`tool_call`（工具调用卡片，含结果/失败态）、`system`（系统通知）。

自定义 part 事件载荷：插件在 `stream_chunk`/`tool_result` 等事件中携带
`part_type` 字段（缺省 text），前端按注册表分发渲染；未注册类型 → 丢弃留痕 +
告警（fail-closed）。

---

## 9. 测试与门禁

- **契约机械闸**：`config/kernel_capabilities/streaming.json` ↔ 内核校验执行器
  一致（kernel_capabilities tests，与 chat.json 同款机械闸）。
- **事件序列测试**（前端 vitest）：start→thinking→chunk→tool→new_message 多事件
  序列断言 store 终态；含"user 不消失、part 不错位"回归用例（2026-08-22 用户
  报告的症状）。
- **对账矩阵测试**：本地/远端状态矩阵（sending/streaming/completed × 命中/未命中）。
- **插件接入端到端**：假插件按 §5 发射事件 → 前端渲染断言。
- 契约冻结：协议定型后按 0.2 契约冻结原则（能不动就不动），动契约需 ADR +
  兼容机制。

---

## 附录 A：LLM 路径事件序列（参考）

```text
用户发送消息
  → 前端 upsert user 乐观消息（id=cmid, status='sending'）
  → ws user_input（带 client_message_id）
内核 dispatch_user_input
  → stream_start { pipeline_id: route_id, message_id: a_<uuid> }   ← 建占位
  → 引擎执行（sidecar llm_core 流式）
      → thinking_start / thinking_chunk* / thinking_end
      → tool_start / tool_result*
      → stream_chunk*
  → new_message {
      pipeline_id, message_id,
      client_message_id,            ← 认领键
      user_message { id, content, sequence, metadata.client_message_id },  ← user 权威版（认领回传）
      message { ...assistant 完整形态 }, sequence
    }
  → stream_end { final_sequence }   ← 收尾兜底
```

同一事件序列对插件路径完全适用——插件替换发射源（自持 message_id、自定 persist）
即可，协议与前端消费逻辑零差异。
