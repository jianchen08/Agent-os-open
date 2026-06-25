# StreamBridge 升级方案：统一消息经纪人 + 多通道适配

**文档版本**: v1.1 | **日期**: 2026-06-02 | **状态**: 方案阶段（含统一点/删除清单 + 分步测试方案）

---

## 目录

1. [问题分析](#1-问题分析)
2. [改造目标](#2-改造目标)
3. [核心架构变更](#3-核心架构变更)
4. [改动清单](#4-改动清单)
5. [多通道适配设计](#5-多通道适配设计)
6. [前后端数据一致性](#6-前后端数据一致性)
7. [耦合解耦清单](#7-耦合解耦清单)
8. [改动影响面评估](#8-改动影响面评估)
9. [迁移步骤](#9-迁移步骤)
10. [风险与回滚](#10-风险与回滚)
11. [统一点与删除清单](#11-统一点与删除清单)
12. [分步测试方案](#12-分步测试方案)

---

## 1. 问题分析

### 1.1 根本矛盾

当前架构有两个根本矛盾：

**矛盾一：消息被两条独立路径送到前端，没有统一的"消息经纪人"**

```
系统通知: TaskNotifier → send_pipeline_message → WS直接推 或 bridge缓冲
流式输出: Engine._run_loop → on_chunk → bridge._queue → drain_loop → WS推送
```

两条路径互不知道对方在做什么。drain_loop 不知道系统通知正在被推送，send_pipeline_message 只知道"bridge 是否在流式"这一个布尔值来判断要不要缓冲。结果是——系统通知到前端气泡和到 AI 大脑是两条不同步的路径（P0-1），stream_end 和 new_message 是竞争关系需要三重防御（P0-3），系统通知推送有 4 条降级路径行为不一致（P0-5）。

**矛盾二：多个组件各自维护自己的消息缓冲/队列，缺乏全局序列号**

```
send_pipeline_message:  _pending_notifications（bridge级别，dict列表）
Engine:                _pending_notifications（引擎级别，str列表）
PipelineEntry:         _next_sequence（管道级别）
StreamBridge:          _queue（bridge级别，dict）
pipelineMessageStore:  parts[]（前端级别）
```

每个组件都有自己的队列和计数器，它们的协调靠启发式规则和兜底补丁——重连补漏有 4 条 sessionId fallback（P0-4），engine 的注入行为在 running 和 suspended 状态下不对称（P1-2），通知元数据在 `list[str]` 中丢失（P1-1）。

### 1.2 18 个关键耦合点

| 编号 | 位置 | 耦合描述 | 严重度 |
|------|------|---------|--------|
| C1 | message_bus → bridge._stream_started | 直接读内部字段判断流式状态 | 🔴 |
| C2 | message_bus → bridge._pending_notifications | 直接写内部队列 | 🔴 |
| C3 | message_bus → TargetedSink | 直接创建 sink 推送通知 | 🔴 |
| C4 | message_bus → engine.inject_message | 推送通知的同时注入引擎 | 🟡 |
| C5 | message_bus → _auto_complete_interaction | 交互逻辑散落在 message_bus | 🟡 |
| C6 | engine._pending_notifications | 引擎自己维护独立的通知队列 | 🔴 |
| C7 | engine._wake_event | 生命周期外部不可见 | 🟡 |
| C8 | engine._suspended_state | 挂起状态散落在多处访问 | 🟡 |
| C9 | bridge → engine._suspended_state["ended"] | 直接改引擎私有字段 | 🔴 |
| C10 | bridge → engine._should_stop | 直接改引擎私有字段 | 🔴 |
| C11 | bridge._entry → PipelineEntry | 注册表引用泄漏 | 🟡 |
| C12 | 前端 stream_end/new_message 双事件协议 | 两个独立事件无因果依赖 | 🔴 |
| C13 | 前端 handleReconnected 4条fallback | 前端负责后端的状态推理 | 🔴 |
| C14 | notify_level 计算逻辑散落 | 3 处地方各自算 level | 🟡 |
| C15 | notificationType 拼接规则不一致 | 不同路径拼法不同 | 🟡 |
| C16 | tool_call / tool_start 双通道 | call_id 生成逻辑不同 | 🟡 |
| C17 | TargetedSink → ws_interaction_notifier | 硬绑 WebSocket，不支持其他通道 | 🟡 |
| C18 | 前端 chunk 超时 120s 盲定时器 | 不感知后端状态 | 🟡 |

---

## 2. 改造目标

### 2.1 核心目标

1. **消灭两条独立路径**：所有消息统一进 bridge，统一出 bridge
2. **消灭多队列**：merge 为 bridge 内单一有序队列 + 全局序列号
3. **前后端一致性**：后端是序列号的唯一权威来源，前端只消费不推理
4. **多通道支持**：新增通道只需写适配器，不改核心链路
5. **不改组件名**：StreamBridge 保持原名，功能升级

### 2.2 非目标

- 不改变 LLM Adapter 的 on_chunk 回调接口
- 不改变 Engine._run_loop 的核心循环结构
- 不改变 pipelineMessageStore 的 parts 渲染模型
- 不改变 ChatInput / router.tsx 的发送路径
- 不改变现有通道适配器的核心逻辑

---

## 3. 核心架构变更

### 3.1 改造前

```
                                ┌──────────────┐
  engine.on_chunk ──────────────►  bridge._queue ├──► drain_loop ──► TargetedSink ──► WS
                                └──────────────┘

  send_pipeline_message ─┬──► bridge._pending_notifications（流式时缓冲）
                          ├──► event_sink.send_event（非流式时直接推）
                          ├──► send_frontend_event（sink 不存在时 fallback）
                          └──► 放弃（全部失败时）
                          │
                          └──► engine.inject_message ──► engine._pending_notifications ──► _run_loop 下次消费

  问题：3 条入口各走各的，4 条降级路径，2 个独立队列
```

### 3.2 改造后

```
  engine.on_chunk ──────────────┐
  bridge.enqueue_notification ──┤  ← 新增：系统通知统一入口
  engine.inject_message ────────┘  ← 简化：仅处理挂起态，运行态委托 bridge
            │
            ▼
     bridge._queue（唯一有序队列）
     │   └─ 每个消息：{type, source, content, sequence, ...}
     │
     ▼
     drain_loop（有序消费）
     │
     ├──► WS 推送（有序，带 sequence）
     │     └─ stream_start → chunks → stream_end(persisted=true) → 通知刷出
     │
     ├──► engine 回调（LLM 注入）
     │     └─ inject_callback(content, source) → state.user_input
     │
     ├──► 多通道分发
     │     └─ MultiChannelSink → CLI / 飞书 / 钉钉 / 企微 / ...
     │
     └──► 持久化
           └─ send_new_message(sequence) → execution_record_storage
```

### 3.3 新消息生命周期

```
入队 → 分配全局sequence → drain_loop 消费 → {
    text/thinking/tool:  格式化 → WS推送(实时)
    notification:        缓冲 → engine注入(回调) → WS推送(stream_end后)
    system chunk:        缓冲 → WS推送(stream_end后)
}
→ drain_loop 退出 → {
    ① 关闭 thinking
    ② 持久化(send_new_message, 携带最终sequence)
    ③ 发 stream_end(persisted=true, final_sequence=N)
    ④ 刷出缓冲通知(system_notification, 带notification_id)
    ⑤ 清理状态
}
```

---

## 4. 改动清单

### 4.1 stream_bridge.py — 核心改造（+80行 / -30行）

| 改动项 | 描述 | 解决耦合 |
|--------|------|---------|
| 新增 `enqueue_notification(content, source)` | 系统通知的统一入口，替代 message_bus 里的 4 条路径 | C1, C2, C3, C14, C15 |
| 新增 `UnifiedEnvelope` 数据结构 | 替代当前裸 dict 的 chunk，统一携带 source/sequence/timestamp 等元数据 | C6 |
| 新增 `set_engine_callbacks(inject, wake, stop)` | bridge 与 engine 的交互改为回调接口，不再直接访问 engine 内部字段 | C9, C10 |
| 新增 `consume_engine_notifications()` | engine 从 bridge 拉取通知，不再自己维护队列 | C6 |
| 合并通知推送路径 | `_handle_chunk` 新增 `notification` 类型，统一缓冲 → 刷出逻辑 | C2, C14, C15 |
| drain_loop 退出顺序调整 | 先持久化 → 再发 stream_end（携带 persisted=true + final_sequence）→ 最后刷通知 | C12 |
| DRAIN-AUTOFIX 简化 | sink 检查逻辑不再依赖 engine_task 状态 | — |
| 删除 stream_start 前的"保留通知刷出" | 上一轮残留通知改为 drain_loop 初始化时消费 | — |

### 4.2 engine.py — 删减为主（-50行）

| 改动项 | 描述 | 解决耦合 |
|--------|------|---------|
| 删除 `_pending_notifications` 维护 | 引擎不再自己管理通知队列 | C6 |
| `inject_message` 简化 | 只处理挂起态（写入 suspended_state + wake）；运行态委托 bridge 回调 | C4, C7 |
| `consume_pending_notifications` 改为从 bridge 拉取 | 统一通知来源 | C6 |
| 新增 `_on_bridge_notification` 回调 | 注册给 bridge 的 inject_callback，收到通知时注入 state.user_input | C4 |
| 暴露 `request_stop()` 公开方法 | 替代 bridge 直接写 `_should_stop` 和 `_suspended_state["ended"]` | C9, C10 |
| 暴露 `get_suspended_notifications()` | 替代外部直接读 `_suspended_state` | C8 |

### 4.3 message_bus.py — 大幅简化（-50行）

| 改动项 | 描述 | 解决耦合 |
|--------|------|---------|
| 删除 4 条通知降级路径 | 整个 `if _msg_source != "user"` 代码块（约 55 行）替换为一行 `bridge.enqueue_notification` | C1, C2, C3, C14, C15 |
| 合并 `_auto_complete_interaction` | 交互完成逻辑移到 bridge 内部 | C5 |
| `send_pipeline_message` 返回值简化 | InjectResult 中保留 method（start/wake/notification/revive） | — |

### 4.4 前端 — 删除防御代码（-90行）

| 文件 | 改动项 | 描述 | 解决耦合 |
|------|--------|------|---------|
| lifecycleHandlers.ts | `handleSystemNotification` | 改为按 `notification_id` 去重（替代精确内容匹配）；删除孤儿占位符清理 | C13, P1-4 |
| lifecycleHandlers.ts | `handleReconnected` | 删除 4 条 sessionId fallback；改为接收后端推送的 `missed_messages` 事件 → 直接 fetch | C13 |
| streamHandler.ts | `handleStreamEnd` | stream_end 携带 `persisted=true` 时直接标记 completed；删除空占位符保留逻辑 | C12 |
| messageHandler.ts | `handleNewMessage` | 删除孤儿清理；删除从事件重建消息逻辑（不再需要，stream_end 已保证持久化） | C12 |
| chunkTimeout.ts | 超时策略 | 改为依赖 stream_keepalive 心跳，不再用 120s 盲定时器 | C18 |

### 4.5 不改的文件

- `pipeline/registry.py` — PipelineEntry 和 EngineRegistry 结构不变
- `pipeline/event_bus.py` — 管道级事件总线不变
- `channels/websocket/server.py` — WS 服务器不变
- `channels/websocket/protocol.py` — WS 协议格式不变
- `channels/websocket/session_manager.py` — 新增 `send_missed_messages` 方法（+20行），其余不变
- `ws_handler.py` — ws_interaction_notifier 单例不变
- `infrastructure/task_notifier.py` — send_pipeline_message 调用方不变（接口兼容）
- `infrastructure/task_idle_timer.py` — 同上
- `plugins/output/task_reminder/plugin.py` — 同上
- `triggers/manager.py` — 同上
- `frontend/src/types/` — 类型定义不变（parts 模型不变）
- `frontend/src/stores/pipelineMessageStore.ts` — 不变
- `frontend/src/components/chat/` — 渲染组件不变

---

## 5. 多通道适配设计

### 5.1 设计原则

1. **bridge 不知道通道存在**：bridge 只产出内部事件，不关心谁消费
2. **每个通道是一个 IOutputSink 实现**：两个方法（send_event + sink_id）
3. **新增通道 = 写适配器 + 一行注册**：不改 bridge/engine/message_bus
4. **按 pipeline_id 自动路由**：不全局广播，只推送给关联管道所属的通道

### 5.2 架构

```
bridge.drain_loop
    │
    output_sink: MultiChannelSink
    │
    ├── Channel[ws]:
    │     TargetedSink（已有，不改）
    │     → WebSocket 协议事件 → 前端浏览器
    │
    ├── Channel[cli]:
    │     CLIOutputAdapter（现有，加 send_event 方法）
    │     → 只订阅 notification 类型 → 终端 [系统] 前缀输出
    │
    ├── Channel[feishu]:
    │     FeishuOutputAdapter（现有，加 send_event 方法）
    │     → notification → 飞书卡片消息
    │     → text_chunk → 飞书消息流
    │
    ├── Channel[dingtalk]:
    │     DingTalkOutputAdapter（同上）
    │
    └── Channel[wecom]:
          WeComOutputAdapter（同上）
```

### 5.3 MultiChannelSink 职责

| 功能 | 描述 |
|------|------|
| 通道注册 | `register(channel_type, adapter)` — 一行注册 |
| 事件分发 | `send_event(event)` → 遍历所有 active 通道 → 各 adapter.send_event |
| 路由策略 | 通过 event 中的 pipeline_id → session_manager 查找关联通道 → 只发给关联通道 |
| 死亡检测 | 单个通道 dead 不影响其他通道；全部 dead 才返回 `is_dead=true` |
| 通知订阅 | 每个通道声明自己订阅的事件类型（如 CLI 只订阅 notification） |

### 5.4 新增通道的工作量

| 步骤 | 工作内容 | 代码量 |
|------|---------|--------|
| 1. 实现 IOutputSink 协议 | send_event(event) → 翻译为通道格式 → 发送 | ~30 行 |
| 2. 声明订阅类型 | 过滤不需要的事件（如 CLI 不需要 stream_chunk） | ~5 行 |
| 3. 注册到 MultiChannelSink | 一行 register 调用 | 1 行 |
| 4. bridge/engine/message_bus 改动 | **0 行** | — |

### 5.5 CLI 通道的特殊处理

CLI 的 stream_chunk/thinking 已经通过 on_chunk 实时渲染。bridge 给 CLI 只推送两种事件：
- `notification` → 终端显示 `[系统] 子任务 'xxx' 已完成`
- `pipeline_suspended` → 终端显示挂起状态

其他事件（text_chunk, thinking, tool_start）由 CLI 的 on_chunk 路径直接处理，不进 bridge。这是"双轨制"：实时输出走 on_chunk，通知和状态变更走 bridge。

---

## 6. 前后端数据一致性

### 6.1 一致性保证链

```
                                                                       
  PipelineEntry._next_sequence                                         
  └─ 管道级全局递增计数器（单写者，无竞争）                               
        │                                                              
        ▼                                                              
  bridge._queue                                                        
  └─ 所有消息按入队时间分配 sequence                                    
  └─ drain_loop 按 sequence 有序消费（FIFO 队列保证）                    
        │                                                              
        ▼                                                              
  drain_loop 退出协议（严格顺序保证）                                     
  └─ ① 关闭 thinking                                                   
  └─ ② 持久化（send_new_message，携带最终 sequence）                    
  └─ ③ stream_end（persisted=true, final_sequence=N）                  
  └─ ④ 刷出通知（system_notification，sequence=M，notification_id）     
  └─ 保证：持久化完成 < stream_end 发出 < 通知发出                       
        │                                                              
        ▼                                                              
  前端接收                                                              
  └─ 所有消息按 sequence 排序                                           
  └─ stream_end.persisted=true → 直接标记 completed                     
  └─ 通知按 notification_id 精确去重                                    
  └─ 验证：收到消息数 = final_sequence 期望值 → 可检测丢消息              
                                                                       
```

### 6.2 前后端验证点

| 验证点 | 后端保证 | 前端验证 |
|--------|---------|---------|
| 消息顺序 | sequence 全局递增 | 收到的消息 sequence 连续不跳号 |
| 消息完整 | drain_loop 不丢消息（队列 → 持久化 → 才发 stream_end） | stream_end.final_sequence = 期望总数 |
| 通知去重 | notification_id 唯一 | 前端按 notification_id 去重，不靠内容 |
| 会话恢复 | session_manager 维护 missed_messages | 重连时后端推送 cursor，前端补漏 |

### 6.3 降级场景

| 场景 | 行为 |
|------|------|
| drain_loop 被 cancel | CancelledError 捕获 → 仍发 stream_end（携带 cancelled=true） |
| 持久化失败 | stream_end 携带 persisted=false → 前端标记 error 而非 completed |
| sink dead | 发 stream_end(connection_lost=true) → 通知引擎停止 |
| stream_start 发送失败 | drain_loop 不进入主循环，直接 return |
| engine_task 异常 | drain_loop 正常退出（engine_task.done=true），正常走退出协议 |

---

## 7. 耦合解耦清单

| 耦合 | 原状态 | 新状态 | 方式 |
|------|--------|--------|------|
| C1 | message_bus 读 bridge._stream_started | **消除** | bridge.enqueue_notification 内部判断 |
| C2 | message_bus 写 bridge._pending_notifications | **消除** | 同上 |
| C3 | message_bus 直接创建 TargetedSink | **消除** | bridge 内部管理 sink |
| C4 | message_bus → engine.inject_message | **改为回调** | bridge._engine_inject_callback |
| C5 | message_bus → _auto_complete_interaction | **收归 bridge** | bridge 内部处理 |
| C6 | engine._pending_notifications | **消除** | engine 从 bridge 拉取 |
| C7 | engine._wake_event 外部不可见 | **改为回调** | bridge._engine_wake_callback |
| C8 | engine._suspended_state 散落访问 | **收归 engine** | engine.get_suspended_notifications() |
| C9 | bridge → engine._suspended_state["ended"] | **改为公开方法** | engine.request_stop() |
| C10 | bridge → engine._should_stop | **同上** | 同上 |
| C11 | bridge._entry 注册表引用 | **解除直引** | 通过回调获取 sequence |
| C12 | stream_end/new_message 双事件 | **合并为一个** | stream_end 携带 persisted=true |
| C13 | 前端 4 条 fallback | **消除** | 后端推送 missed_messages |
| C14 | notify_level 计算散落 | **收归 bridge** | bridge 内部统一计算 |
| C15 | notificationType 拼接不一致 | **同上** | 同上 |
| C16 | tool_call/tool_start 双通道 | **不改** | 保持现状，仅加文档注释 |
| C17 | TargetedSink 硬绑 WS | **改为多通道** | MultiChannelSink |
| C18 | 前端 120s 盲定时器 | **改为心跳依赖** | 依赖 stream_keepalive |

---

## 8. 改动影响面评估

### 8.1 按文件统计

| 文件 | 类型 | 新增 | 删除 | 修改 | 风险 |
|------|------|------|------|------|------|
| `stream_bridge.py` | 核心 | +80 | -30 | 8处方法 | 🟡 中 |
| `engine.py` | 核心 | +20 | -50 | 4处方法 | 🟡 中 |
| `message_bus.py` | 路由 | +5 | -55 | 2处方法 | 🟢 低 |
| `session_manager.py` | WS | +20 | 0 | 1处方法 | 🟢 低 |
| `lifecycleHandlers.ts` | 前端 | +10 | -40 | 3处方法 | 🟡 中 |
| `streamHandler.ts` | 前端 | 0 | -20 | 1处方法 | 🟢 低 |
| `messageHandler.ts` | 前端 | 0 | -30 | 1处方法 | 🟢 低 |
| `chunkTimeout.ts` | 前端 | +5 | -10 | 1处方法 | 🟢 低 |
| **总计** | — | **+140** | **-235** | **21处** | — |

**净减少约 95 行。加上后续清理（统一点 → 删除清单），累计删除约 450 行冗余代码。**

### 8.2 测试影响

| 测试范围 | 原有测试 | 需要新增 | 说明 |
|---------|---------|---------|------|
| StreamBridge | 已有 drain_loop 测试 | 新增 enqueue_notification 测试；notification chunk 处理测试 | 核心变更需要覆盖 |
| Engine | 已有 inject_message 测试 | 新增 从 bridge 拉取通知 测试；stop 回调测试 | 删除了 _pending_notifications |
| message_bus | 已有 send_pipeline_message 测试 | 更新 系统通知路径测试（简化后） | 接口兼容，测试小幅调整 |
| 前端 handler | 已有 streamEnd/streamError 测试 | 更新 stream_end persisted 路径测试 | 兼容性：旧版不传 persisted 仍正常 |
| 多通道 | 无 | 新增 MultiChannelSink 测试；通道注册/分发测试 | 新功能 |
| 端到端 | 已有 CLI E2E | 新增 多通道并发推送 E2E | 验证隔离性 |

---

## 9. 迁移步骤

### 阶段一：向后兼容准备（1天）

- [ ] 在 `stream_end` 事件中新增 `persisted` 和 `final_sequence` 字段（可选，不传时前端保持旧行为）
- [ ] 前端 `handleStreamEnd` 新增 `persisted=true` 路径（兼容旧版不传的情况）
- [ ] `message_bus.system_notification` 路径新增 `notification_id` 字段
- [ ] 前端 `handleSystemNotification` 新增 `notification_id` 去重（兼容旧版不传的情况）
- [ ] 全量测试套件通过

### 阶段二：bridge 升级（2天）

- [ ] 实现 `UnifiedEnvelope` 数据结构
- [ ] 实现 `enqueue_notification` 方法
- [ ] 实现 drain_loop 退出顺序变更（先持久化再 stream_end）
- [ ] 实现 `set_engine_callbacks`
- [ ] engine.inject_message 简化（仅处理挂起态）
- [ ] engine.consume_pending_notifications 改为从 bridge 拉取
- [ ] 删除 engine._pending_notifications
- [ ] message_bus 通知路径替换为 bridge.enqueue_notification
- [ ] message_bus 删除 4 条降级路径
- [ ] 全量测试 + E2E 通过

### 阶段三：多通道适配（1天）

- [ ] 实现 MultiChannelSink
- [ ] TargetedSink 注册为 ws 通道
- [ ] CLIOutputAdapter 实现 IOutputSink（仅订阅 notification）
- [ ] 全量测试通过

### 阶段四：前端清理（1天）

- [ ] 删除 handleNewMessage 孤儿清理 + 重建逻辑
- [ ] 删除 handleReconnected 的 4 条 fallback
- [ ] 后端 session_manager 新增 miss_messages 推送
- [ ] 删除 handleStreamEnd 空占位符保留逻辑
- [ ] chunk 超时改为心跳依赖
- [ ] 全量测试 + 前端 E2E 通过

### 阶段五：清理与文档（0.5天）

- [ ] 删除 pipeline_received 空操作（后端 _send_received_event + 前端 handlePipelineReceived）
- [ ] 统一 Markdown 渲染器（可选）
- [ ] 更新 stream_bridge.py 模块文档
- [ ] 更新 pipeline/MEMORY.md

---

## 10. 风险与回滚

### 10.1 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| drain_loop 退出顺序变更导致 stream_end 延迟 | 低 | 中 | 持久化是异步操作，延迟 < 50ms |
| 持久化失败导致 stream_end 永远不发 | 低 | 高 | 持久化失败发 stream_end(persisted=false)，前端标记 error |
| engine 回调注册时机不对导致通知丢失 | 中 | 中 | bridge 启动时强制检查回调；callback=None 时降级为直接注入 |
| 多通道并发冲突 | 低 | 低 | 每个通道独立 sink，互不干扰 |
| 前端兼容性（旧版客户端） | 中 | 低 | 新增字段均为可选，旧版忽略新字段 |

### 10.2 回滚策略

核心改动集中在两个文件：`stream_bridge.py` 和 `engine.py`。

- **stream_bridge.py 回滚**：恢复 drain_loop 的旧退出顺序（先 stream_end 再 send_new_message），恢复旧的 `_handle_chunk`（删除 notification 分支）
- **engine.py 回滚**：恢复 `_pending_notifications`，恢复 `inject_message` 的旧逻辑
- **message_bus.py 回滚**：恢复 4 条通知降级路径
- **前端回滚**：恢复 handleStreamEnd 空占位符逻辑，恢复 handleNewMessage 重建逻辑

所有改动都是可独立回滚的，不涉及数据库 schema 变更或协议不兼容变更。

### 10.3 灰度策略

建议按通道灰度：
1. 先在 WebSocket 前端启用（覆盖面最大，问题最先暴露）
2. 验证通过后开启 CLI 通道
3. 最后开启飞书/钉钉/企微通道

每个阶段观察 1-2 天，确认无回归后推进下一步。

---

## 附录

### A. 最终架构全景

```
                        ┌─────────────────────────────────────────┐
                        │         用户的输入来自多个通道            │
                        │   WebSocket │ CLI │ 飞书 │ 钉钉 │ 企微   │
                        └────────────┬────────────────────────────┘
                                     │
                                     ▼
                        ┌────────────────────────┐
                        │   send_pipeline_message │  ← 统一入口
                        │   (message_bus.py)      │
                        └────────────┬───────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
            idle: 启动引擎      running/suspended:   无引擎:
            创建bridge        inject_message    _try_revive
                    │                │                │
                    └────────────────┼────────────────┘
                                     │
                                     ▼
                        ┌────────────────────────┐
                        │   PipelineStreamBridge  │  ← 统一消息经纪人
                        │   ┌──────────────────┐  │
                        │   │  单一有序队列     │  │
                        │   │  全局序列号      │  │
                        │   │  drain_loop 消费  │  │
                        │   └──────┬───────────┘  │
                        └──────────┼──────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        WS 推送               engine 回调           持久化
        (有序+序列号)         (LLM 注入)         (带序列号)
              │                    │
              ▼                    ▼
     MultiChannelSink       _run_loop 消费
     ┌──────┼──────┐
     ▼      ▼      ▼
    WS    CLI    飞书...
```

### B. 问题解决对照

| 原问题 | 解决方式 | 改造前 | 改造后 |
|--------|---------|--------|--------|
| P0-1 通知前后端不同步 | 统一进 bridge，顺序保证 | 先推 WS 再注引擎 | 同一 chunk 触发两者 |
| P0-2 DRAIN-AUTOFIX 竞态 | 简化为回调检查 | on_chunk 查 registry | registry 回调 |
| P0-3 stream_end/new_message 竞态 | drain_loop 先持久化再 stream_end | 两个独立事件 | 一个事件带 persisted |
| P0-4 重连补漏 4 条 fallback | 后端推送 missed_messages | 前端推理 4 层 | 后端告知 cursor |
| P0-5 通知 4 条降级路径 | 合并为 enqueue_notification | 4 条路径 | 1 个入口 |
| P1-1 通知丢失元数据 | UnifiedEnvelope 结构化 | list[str] | dataclass |
| P1-2 注入路径不对称 | 运行态统一走 bridge 回调 | 两套逻辑 | 一套逻辑 |
| P1-4 通知去重仅内容匹配 | notification_id 去重 | 精确内容 | 唯一 ID |
| P1-5 任务提醒不走气泡 | 统一走 bridge notification | 两条渲染路径 | 一条 |
| P1-6 pipeline_received 空操作 | 删除 | 空操作 | 不存在 |
| P1-7 chunk 超时盲定时器 | 依赖心跳 keepalive | 120s 盲定时 | 后端心跳 |

---

## 11. 统一点与删除清单

每行"统一"对应一行"删除"——"负代码"是好事，每删一行就少一个维护负担。删除总计：**约 450 行**。

| # | 统一了什么 | 从哪里统一到哪里 | 要删除的代码 | 位置 | 行数 |
|---|-----------|----------------|-------------|------|------|
| 1 | 通知推送路径 | 4条 → bridge.enqueue_notification | `if _msg_source != "user"` 整个代码块（含3种level计算、3种notificationType拼接、_event_sink三级查找） | message_bus.py L214-309 | **-96** |
| 2 | 通知缓冲队列 | engine._pending_notifications + bridge._pending_notifications → bridge._pending_notifications | `self._pending_notifications: list[str]` 属性及所有维护代码 | engine.py L112 | **-1** |
| 3 | 通知消费 | engine 自己消费 → engine 从 bridge 拉取 | `consume_pending_notifications` 完整实现 + `_run_loop` 中3处调用改造 | engine.py L1067-1077 | **-11** |
| 4 | 运行态注入 | engine.inject_message running 路径 → bridge 回调 | running 态分支（`_pending_notifications.append` + `_try_cancel_pending_interaction`） | engine.py L1133-1142 | **-10** |
| 5 | event_sink 获取 | 3级 fallback → bridge 自己管理 | `_event_sink` 三级查找逻辑（参数→registry bridge→_create_sink） | message_bus.py L202-212 | **-11** |
| 6 | _auto_complete_interaction | message_bus 独立调用 → bridge 内部回调 | `_auto_complete_interaction` 函数 + 调用点 | message_bus.py L92-115 + L423 | **-25** |
| 7 | 引擎停止 | bridge 直写 engine 内部 → engine.request_stop() | `_engine._suspended_state["ended"]=True`; `_engine._should_stop=True`; `_notify_engine_sink_dead` 中的引擎内部操作 | stream_bridge.py L295-340 | **-46** |
| 8 | 持久化位置 | _start_bg_drain finally 单独 send_new_message → drain_loop 内部 | `_drain_and_cleanup` finally 中的独立 `send_new_message` 调用 | message_bus.py L784-803 | **-20** |
| 9 | 重连补漏 | 前端4条fallback → 后端推送 missed_messages | `handleReconnected` 完整函数（streamingState遍历 + messagesByPipeline遍历 + stuckMessages清理 + 3层sessionId fallback） | lifecycleHandlers.ts L107-225 | **-119** |
| 10 | 重连轮询 | setTimeout 轮询 → 后端主动推送 | `_startReconnectPolling` 函数 | lifecycleHandlers.ts L60-92 | **-33** |
| 11 | 孤儿占位符清理 | stream_end/new_message/system_notification 3处 → bridge 顺序保证 | `handleNewMessage` 孤儿清理; `handleSystemNotification` 孤儿清理 | messageHandler.ts L86-101 + lifecycleHandlers.ts L376-390 | **-30** |
| 12 | 消息重建兜底 | new_message 找不到 → stream_end 保证持久化 | `handleNewMessage` 从事件重建消息的分支 | messageHandler.ts L63-81 | **-19** |
| 13 | 空占位符保留 | stream_end 保留等 new_message → 直接标记完成 | `handleStreamEnd` 空消息分支（原 removeMessage → 保留 → 不再需要） | streamHandler.ts L203-224 | **-22** |
| 14 | chunk 超时 | 120s 盲定时器 → 依赖 stream_keepalive | `chunkTimeout.ts` 主要逻辑；保留 reset 改为心跳触发 | chunkTimeout.ts | **-25** |
| 15 | pipeline_received | 永远空操作 → 删除 | `_send_received_event` + 6处调用; `handlePipelineReceived` | message_bus.py + lifecycleHandlers.ts | **-41** |
| 16 | 前端通知去重 | 精确内容匹配 → notification_id | `alreadyExists` 精确匹配（`m.content === content`）改为 id 去重 | lifecycleHandlers.ts L333-337 | **-5** |
| 17 | stream_start 前刷旧通知 | drain_loop 初始化时处理 → 统一在退出时刷 | `_pending_notifications` 保留 + 刷出 + 重建队列的复杂逻辑 | stream_bridge.py L680-689 | **-10** |
| | | | | **总计** | **~ -450** |

---

## 12. 分步测试方案

**原则**：每个子步骤独立可测、独立可提交、独立可回滚。禁止一次改多个文件再跑测试。

### 阶段 0：基线（现在）

- [ ] 全量单元测试：确保当前 793 passed, 36 skipped
- [ ] 全量 E2E：确保当前 87 passed
- [ ] 记录测试基线快照

---

### 阶段 1：协议向后兼容扩展（不改变行为）

**目标**：给现有事件加新字段，前端兼容新旧两种格式。所有测试必须通过。

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 1a | stream_end 新增可选字段 `persisted`、`final_sequence` | 单元测试：旧格式正常处理；新格式不改变业务逻辑 | stream_bridge.py + streamHandler.ts |
| 1b | system_notification 新增可选字段 `notification_id` | 单元测试：新字段出现时按 id 去重；不出现时保持旧行为 | stream_bridge.py + lifecycleHandlers.ts |
| 1c | 前端兼容性 | 单元测试：不传新字段的旧事件仍正常渲染 | streamHandler.ts + lifecycleHandlers.ts |
| 1d | 提交 + 全量测试 | 793 passed | — |

---

### 阶段 2：bridge 数据结构升级（内部重构）

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 2a | UnifiedEnvelope 数据结构 + bridge._queue 类型变更 | 单元测试：on_chunk → enqueue → drain_loop 完整链路 | stream_bridge.py |
| 2b | _handle_chunk 新增 notification 类型枚举（暂不触发） | 单元测试：新增类型不影响现有 chunk 处理 | stream_bridge.py |
| 2c | 提交 + 全量测试 | 793 passed | — |

---

### 阶段 3：drain_loop 退出顺序变更（关键变更）

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 3a | 退出协议：先 send_new_message → stream_end(persisted=true) → 刷通知 | 单元测试：正常退出/超时退出/sink dead 退出/cancel 退出 4 条路径 | stream_bridge.py |
| 3b | 删除 _start_bg_drain finally 中的独立 send_new_message | 集成测试：new_message 事件只发一次 | message_bus.py |
| 3c | 提交 + 全量测试 | 793 passed | — |
| 3d | **E2E 真实调用**：验证 stream_end 携带 persisted=true | CLI E2E + WS E2E | — |

---

### 阶段 4：engine 解耦

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 4a | engine 新增 `request_stop()` 公开方法 | 单元测试：调用后 _should_stop 置为 True | engine.py |
| 4b | bridge 新增 `set_engine_callbacks` + 回调调用链路 | 单元测试：回调注册 + 调用成功 | stream_bridge.py |
| 4c | bridge._notify_engine_sink_dead 改为调回调 | 单元测试：sink dead → engine 收到 stop | stream_bridge.py |
| 4d | engine.inject_message 简化：仅处理挂起态；运行态走回调 | 单元测试：挂起态注入正确；运行态注入走 bridge | engine.py |
| 4e | engine.consume_pending_notifications 改为从 bridge 拉取 | 集成测试：通知注入后 engine 正确获取 | engine.py |
| 4f | **删除 engine._pending_notifications** | 全量测试：删除后无 import/引用报错 | engine.py |
| 4g | 提交 + E2E | 消息 → 通知 → 引擎消费并回复 | — |

---

### 阶段 5：message_bus 简化

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 5a | `if _msg_source != "user"` 代码块替换为 `bridge.enqueue_notification` | 单元测试：system 来源消息正确入 bridge 队列 | message_bus.py |
| 5b | 删除 _event_sink 三级查找 + 通知降级路径 | 全量测试：无 import 错误 | message_bus.py |
| 5c | 删除 _send_received_event → pipeline_received | 全量测试 | message_bus.py |
| 5d | 删除 _auto_complete_interaction 函数 + 调用 | 全量测试 | message_bus.py |
| 5e | 提交 + E2E | 子任务完成通知正常推送 + 引擎收到 | — |

---

### 阶段 6：前端清理

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 6a | handleStreamEnd：persisted=true → 直接 completed | 单元测试：mock stream_end 事件 | streamHandler.ts |
| 6b | handleNewMessage：删除孤儿清理 + 重建逻辑 | 集成测试：正常消息流无孤儿 | messageHandler.ts |
| 6c | handleSystemNotification：改为 notification_id 去重 | 单元测试：同 id 重复事件只创建一条 | lifecycleHandlers.ts |
| 6d | handleReconnected：删除 4 条 fallback + 轮询 | 单元测试：mock 重连 + missed_messages | lifecycleHandlers.ts |
| 6e | chunkTimeout：改为心跳依赖 | 单元测试：120s 无 keepalive → 超时；有 → 不超时 | chunkTimeout.ts |
| 6f | 删除 handlePipelineReceived | 全量测试 | lifecycleHandlers.ts |
| 6g | 提交 + E2E | 发送 → 通知 → 重连 三条完整链路 | — |

---

### 阶段 7：多通道（独立模块）

| 子步骤 | 改动 | 验证方式 | 文件 |
|--------|------|---------|------|
| 7a | MultiChannelSink 实现 | 单元测试：注册/分发/死亡检测 | stream_bridge.py（新增类） |
| 7b | TargetedSink 注册为 ws 通道 | 集成测试：WS 通道正常推送 | stream_bridge.py |
| 7c | CLIOutputAdapter 实现 IOutputSink（仅订阅 notification） | 单元测试：notification 事件 → [系统] 前缀 | cli/output_adapter.py |
| 7d | 提交 + E2E | CLI 收到通知；WS 不受影响 | — |

---

### 阶段 8：清理收尾

| 子步骤 | 改动 | 验证方式 |
|--------|------|---------|
| 8a | 删除所有不再使用的 import | 全量测试 |
| 8b | 删除标记为 deprecated 的方法 | 全量测试 |
| 8c | 更新模块 docstring | — |
| 8d | 更新 MEMORY.md 记录 | — |
| 8e | 最终全量测试 | 793 passed, 87 E2E passed |

---

### 每步检查清单

```
□ 运行相关模块的单元测试（pytest path/to/module -v）
□ 运行全量单元测试（pytest -v）
□ 运行集成测试（pytest -m integration --run-integration -v）
□ 运行 E2E 测试（仅阶段 3d、4g、5e、6g、7d 需要）
□ 检查日志输出无新增 ERROR
□ 检查 Python import 无循环依赖
□ git commit（每子步骤独立提交，标注改了什么 + 删了什么）
```

### 失败处理策略

- **不回退已提交的步骤**（它们已被验证通过）
- **只在当前子步骤内修复**
- 修复后只重跑当前模块的单元测试 + 全量回归
- 如果修复涉及前面已提交的代码，开新子步骤，不修改已提交 commit
- **总计约 30 个子步骤，每步 5-30 行改动，测试跑通再进下一步**

