/** 统一管道消息状态管理 Store 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { getMessageSegments } from '@/services/api/messageSegments'
import { getMessages as apiGetMessages, mapBackendMessageToMessage } from '@/services/api/session'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useContextKeys } from '@/stores/contextKeysStore'
import {
  _fetchAbortControllers,
  abortKeyOf,
  fetchKindOf,
} from '@/stores/pipelineMessageFetchWindows'
import { buildPersistedSnapshot } from '@/stores/pipelineMessagePersistence'
// retry removed per audit: 内部 API 不应内置重试，429/5xx 重试统一由 axios interceptor 管理
import { decideClaim } from '@/streaming/claim'
import { indexedDbStorage } from '@/utils/indexedDbStorage'
import { loggers } from '@/utils/logger'
import { markMessageHydrated, waitForMessageHydration } from './pipelineMessageHydration'
import {
  buildTransientPlaceholder,
  calculateBottomCursor,
  capMessagesForMemory,
  compareMessages,
  filterBlankMessages,
  isCoveredByApi,
  mergeIncrementalApiWithLocal,
  mergeSorted,
  mutateMessageParts,
} from './pipelineMessageMerge'
import { reconcileFromAPI } from './pipelineMessageReconcile'
import { PIPELINE_MESSAGE_ACCOUNT_RESET, setInitialLoadingFlag } from './pipelineMessageState'
import type { PipelineMessageState, PipelineMeta } from './pipelineMessageState'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'
import type { Message } from '@/types/models'

/** 对外契约不变：compareMessages 仍可从本模块导入（单一真值源在 @/utils/messageOrder，经 pipelineMessageMerge 转出）。 */
export { compareMessages } from './pipelineMessageMerge'

const logger = loggers.sessionStore

/** initFromAPI 保留「飞行中」本地消息的新鲜度窗口（ms）：乐观 user 以 timestamp、
 *  assistant（流式/刚完成）以 _lastUpdated 判定；超窗的视为 persist 残留/断线
 *  残影，按刷新去漂移语义丢弃。窗口需覆盖后端 init 慢读（大会话全量读可达
 *  数十秒），迟到快照才不会抹掉窗口内的本页活动。 */
const INFLIGHT_FRESH_MS = 90_000

export type { PipelineMeta, StreamingStatus } from './pipelineMessageState'
export { PIPELINE_MESSAGE_ACCOUNT_RESET }

/** 统一管道消息 Store */
export const usePipelineMessageStore = create<PipelineMessageState>()(
  persist((set, get) => ({
  ...PIPELINE_MESSAGE_ACCOUNT_RESET,

  /** 注册管道，建立 pipelineId 与元数据的映射 */
  registerPipeline: (meta: PipelineMeta) => {
    set((state) => {
      const existingMeta = state.pipelines[meta.pipelineId]
      // 如果已存在相同 pipelineId，保留已有消息和未读计数
      return {
        pipelines: {
          ...state.pipelines,
          [meta.pipelineId]: existingMeta
            ? { ...existingMeta, ...meta, unreadCount: existingMeta.unreadCount }
            : meta,
        },
        pipelineSessionMap: {
          ...state.pipelineSessionMap,
          [meta.pipelineId]: meta.sessionId,
        },
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [meta.pipelineId]: state.messagesByPipeline[meta.pipelineId] || [],
        },
      }
    })
  },

  /** 激活管道，同时重置该管道的未读计数 */
  activatePipeline: (pipelineId: string) => {
    set((state) => {
      const meta = state.pipelines[pipelineId]
      return {
        activePipelineId: pipelineId,
        pipelines: meta
          ? {
              ...state.pipelines,
              [pipelineId]: { ...meta, unreadCount: 0 },
            }
          : state.pipelines,
      }
    })
  },

  /** 添加消息到指定管道，自动去重和排序 */
  addMessage: (pipelineId: string, message: Message) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const realMessageId = (message as Message & { message_id?: string }).message_id || message.id

      const existingIndex = pipelineMessages.findIndex((m) => m.id === realMessageId)

      let updatedMessages: Message[]
      let unreadChanged = false

      if (existingIndex >= 0) {
        updatedMessages = [...pipelineMessages]
        updatedMessages[existingIndex] = {
          ...pipelineMessages[existingIndex],
          ...message,
          id: pipelineMessages[existingIndex].id,
        }
      } else {
        updatedMessages = [...pipelineMessages, { ...message, id: realMessageId }]
        if (state.activePipelineId !== pipelineId) {
          unreadChanged = true
        }
      }

      const newPipelines = { ...state.pipelines }
      if (unreadChanged && newPipelines[pipelineId]) {
        newPipelines[pipelineId] = {
          ...newPipelines[pipelineId],
          unreadCount: newPipelines[pipelineId].unreadCount + 1,
        }
      }

      // bottomCursor 只由 API 权威路径（initFromAPI / appendMessages / prependMessages）维护。
      // addMessage 是乐观/流式/通知消息的入口（pending 乐观 user、ensureStreamingPlaceholder
      // 流式占位、handleSystemNotification 系统通知、send_failed 错误气泡），其
      // sequence 非后端权威值（无权威 seq 时挂空，本地不再拼号）。
      // 若让它推进 bottomCursor，会污染双游标：
      //   - 系统通知 sequence 被抬到 localMax+1，initFromAPI 重排时被 mergeSorted 排到末尾，
      //     导致「通知跑到 AI 回复后面」（排序错乱）。
      //   - 流式占位的临时 sequence 进入游标，下次 after_sequence=bottomCursor 补漏会跳过
      //     真正未加载的权威消息。
      // 流式期间 bottomCursor 保持不动，等流式结束 → 切 Tab/重连触发 appendMessages 时
      // 由 calculateBottomCursor 写入权威值。

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
          [pipelineId]: capMessagesForMemory(updatedMessages),
        },
        pipelines: newPipelines,
      }
    })
  },

  /** 认领 user 消息（[来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]）。
   *  裁决规则（纯逻辑见 src/streaming/claim.ts）：
   *  - 主数组候选命中 → upgrade：权威 record_id/seq 记入独立 recordId 字段，
   *    UI id 不变（React key 稳定），status='completed'
   *  - 候选缺失 → insert：以 cmid 为 id 补插权威 user 版（后端已持久化，不能丢）
   *  - 已认领/不匹配 → skip（幂等）
   *  乐观 user 消息自发送瞬间就在主数组（单一消息数组）——
   *  认领即原地升级，无 pending 区。 */
  claimUserMessage: (pipelineId, cmid, userRecord) => {
    const state = get()
    // 主数组候选：优先同 cmid（认领过的 recordId 幂等判据在裁决内）
    const mainMsgs = state.messagesByPipeline[pipelineId] || []
    const candidate = mainMsgs.find((m) => m.clientMessageId === cmid)
      || mainMsgs.find((m) => m.recordId === userRecord.id)

    const act = decideClaim(
      candidate
        ? { id: candidate.id, clientMessageId: candidate.clientMessageId, status: candidate.status, recordId: candidate.recordId }
        : undefined,
      userRecord,
      cmid,
    )

    if (act.kind === 'skip') return 'skipped'

    // 认领成功 = 发送态落定点（2026-11-04 落定语义重定义）：三条调用路径
    // （new_message 权威回传 / user_message_confirmed 早回执 / 队列消费
    // consumed_messages）在此单点收敛，按 client_message_id 撤发送态——发送态
    // 终点 = 气泡上屏。未知 cmid（注入消息等非本端发送）在 store 内幂等零动作。
    useChatInputStore.getState().landSendByCmid(cmid)

    set((stateInner) => {
      const msgs = stateInner.messagesByPipeline[pipelineId] || []
      if (act.kind === 'upgrade') {
        const idx = msgs.findIndex((m) => m.id === act.messageId)
        const next = [...msgs]
        if (idx >= 0) {
          next[idx] = {
            ...next[idx],
            recordId: act.recordId,
            status: 'completed',
            ...(act.sequence != null ? { sequence: act.sequence } : {}),
          }
        } else {
          // 候选在主数组不存在（断线期间确认到达/刷新窗口）→ 补插权威版（保留 UI id）
          next.push({
            id: act.messageId,
            sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
            role: 'user',
            content: typeof userRecord.content === 'string' ? userRecord.content : '',
            timestamp: new Date().toISOString(),
            status: 'completed',
            clientMessageId: cmid,
            recordId: act.recordId,
            sequence: act.sequence,
          } as Message)
        }
        return {
          messagesByPipeline: { ...stateInner.messagesByPipeline, [pipelineId]: capMessagesForMemory(next) },
        }
      }
      // insert：以 cmid 为 UI id 补插（权威 user 已持久化，绝不能因本地缺候选而丢）
      if (msgs.some((m) => m.id === act.messageId || m.recordId === act.recordId)) {
        return stateInner
      }
      return {
        messagesByPipeline: {
          ...stateInner.messagesByPipeline,
          [pipelineId]: capMessagesForMemory([
            ...msgs,
            {
              id: act.messageId,
              sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
              role: 'user',
              content: typeof userRecord.content === 'string' ? userRecord.content : '',
              timestamp: new Date().toISOString(),
              status: 'completed',
              clientMessageId: cmid,
              recordId: act.recordId,
              sequence: act.sequence,
            } as Message,
          ]),
        },
      }
    })
    return act.kind === 'upgrade' ? 'upgraded' : 'inserted'
  },

  /** 无 cmid 注入消息（触发器/任务/HTTP）补插 user 气泡（ADR-2026-08-26）：
   *  按 id 幂等；按 sequence 落位（触发器消息出现在 assistant 回复之前）。 */
  ensureInjectedUserMessage: (pipelineId, userRecord) => {
    const recordId = userRecord.id
    if (!recordId) return
    set((stateInner) => {
      const msgs = stateInner.messagesByPipeline[pipelineId] || []
      // 幂等：recordId 或同内容同 seq 已存在 → 跳过（重复事件不双插）
      if (msgs.some((m) => m.recordId === recordId || m.id === recordId)) {
        return stateInner
      }
      const content = typeof userRecord.content === 'string' ? userRecord.content : ''
      const seq = userRecord.sequence
      const msg = {
        id: recordId,
        recordId,
        sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
        role: 'user' as const,
        content,
        timestamp: new Date().toISOString(),
        status: 'completed' as const,
        ...(seq != null ? { sequence: seq } : {}),
        ...(userRecord.metadata ? { metadata: userRecord.metadata } : {}),
      } as Message
      // 按 sequence 落位：有 seq 则插到首个 seq 大于它的消息之前；
      // 无 seq 追加尾部（与后端消息序一致：user 先于其 assistant 回复）。
      let next: Message[]
      if (seq != null) {
        const idx = msgs.findIndex((m) => m.sequence != null && m.sequence > seq)
        next = idx >= 0 ? [...msgs.slice(0, idx), msg, ...msgs.slice(idx)] : [...msgs, msg]
      } else {
        next = [...msgs, msg]
      }
      return {
        messagesByPipeline: {
          ...stateInner.messagesByPipeline,
          [pipelineId]: capMessagesForMemory(next),
        },
      }
    })
  },

  /** 更新指定管道中的消息（部分更新）。精确 ID 匹配：失配即
   * 记 error 并跳过——不做 role+sequence/指纹等模糊匹配（非唯一键，迟到事件
   * 会写错旧消息）。找不到目标说明事件与本地状态脱节，正确处置是等对账补正，
   * 而不是猜一条相近的写。 */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []

      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        logger.error(
          '[updateMessage] 目标消息不存在，跳过更新（精确匹配，不创建不模糊匹配）: pipelineId=%s messageId=%s',
          pipelineId?.slice(0, 12),
          messageId?.slice(0, 12),
        )
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        ...partial,
        _lastUpdated: Date.now(),
      } as Message

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 获取指定管道的消息列表 */
  getMessages: (pipelineId: string) => {
    return get().messagesByPipeline[pipelineId] || []
  },

  /** 移除指定管道中的消息 */
  removeMessage: (pipelineId: string, messageId: string) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (messageIndex < 0) return state
      const updatedMessages = pipelineMessages.filter((_, i) => i !== messageIndex)
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 权威截断（messages_truncated 事件，聊天消息流权威化设计真值不变量 3）：
   *  按 sequence < truncateBeforeSeq 保留，其后（含无 seq 但位于截断点之后的
   *  本地暂态）一并移除；updated_messages 按 seq 原地改写（编辑重发目标消息
   *  权威全文）。幂等：重复事件零变化。挂起的流式态随截断清理（被截后缀的
   *  流式占位不得残留生成态）。 */
  applyTruncation: (pipelineId: string, truncateBeforeSeq: number, updated?: Array<{
    sequence?: number
    content?: unknown
  }>) => {
    set((state) => {
      const msgs = state.messagesByPipeline[pipelineId]
      if (!msgs) return state
      // 无 seq 本地暂态：保留当且仅当它出现在首个被截消息（首个
      // seq >= truncateBeforeSeq）之前——本地顺序是引擎序的投影，边界之后的
      // 暂态（流式占位/未认领乐观消息）属于被截后缀。
      const firstTruncatedIdx = msgs.findIndex(
        (m) => m.sequence != null && m.sequence >= truncateBeforeSeq,
      )
      const kept = msgs.filter((m, i) => {
        if (m.sequence != null) return m.sequence < truncateBeforeSeq
        return firstTruncatedIdx < 0 ? true : i < firstTruncatedIdx
      })
      if (updated) {
        for (const u of updated) {
          if (typeof u.sequence !== 'number') continue
          const idx = kept.findIndex((m) => m.sequence === u.sequence)
          if (idx >= 0 && typeof u.content === 'string') {
            kept[idx] = { ...kept[idx], content: u.content }
          }
        }
      }
      const streaming = state.streamingState[pipelineId]?.isStreaming
        ? { ...state.streamingState }
        : state.streamingState
      if (streaming !== state.streamingState) delete streaming[pipelineId]
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: kept,
        },
        streamingState: streaming,
      }
    })
  },

  /** 拉取段清单写入 spanIndex。失败降级为空清单（‹i/n› 切换器隐藏）+ warn：
   *  段面是增强能力，不阻塞消息主链路；segment_activated 对账与下次打开会话
   *  均会重试，无需在此重试。 */
  fetchSegments: async (pipelineId: string) => {
    try {
      const segments = await getMessageSegments(pipelineId)
      usePipelineMessageStore.setState((s) => ({
        spanIndex: {
          ...s.spanIndex,
          // (base_seq, created_at) 升序：同锚点多代按冻结时间定序，代号 = 序数
          [pipelineId]: [...segments].sort(
            (a, b) => a.base_seq - b.base_seq || a.created_at.localeCompare(b.created_at),
          ),
        },
      }))
    } catch (error) {
      logger.warn(
        '[pipelineMessageStore.fetchSegments] 段清单拉取失败: pipelineId=%s err=%s',
        pipelineId,
        error instanceof Error ? error.message : String(error),
      )
      usePipelineMessageStore.setState((s) => ({
        spanIndex: { ...s.spanIndex, [pipelineId]: [] },
      }))
    }
  },

  /** 某锚点 base_seq 的段列表（created_at 升序；stored 已按 base_seq 排，
   *  此处再排一次保证 getter 单独满足「同 base_seq 按冻结时间定序」契约） */
  getSegmentsAt: (pipelineId: string, baseSeq: number) => {
    return (get().spanIndex[pipelineId] || [])
      .filter((seg) => seg.base_seq === baseSeq)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
  },

  /** 乐观换装：段成员自 base_seq 起点覆写本地消息视图。后缀语义（§2.5）下
   *  激活 = [base_seq..末尾] 整段替换，故保留 base_seq 之前的消息，其后整段
   *  呈现为段成员；成员缺权威 seq 时按 base_seq + 序数补位。游标不更新——
   *  本视图是乐观态，segment_activated 对账的 initFromAPI 权威重建后自洽。 */
  applySegmentView: (pipelineId, detail, sessionId) => {
    set((state) => {
      const msgs = state.messagesByPipeline[pipelineId]
      if (!msgs) return state
      const baseSeq = detail.base_seq
      const kept = msgs.filter((m) => m.sequence == null || m.sequence < baseSeq)
      const members = detail.members.map((raw, i) => {
        const mapped = mapBackendMessageToMessage(raw, sessionId)
        return { ...mapped, sequence: mapped.sequence || baseSeq + i } as Message
      })
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: capMessagesForMemory(
            mergeSorted([...kept].sort(compareMessages), members.sort(compareMessages)),
          ),
        },
        segmentViewByPipeline: {
          ...state.segmentViewByPipeline,
          [pipelineId]: { baseSeq, segmentId: detail.id },
        },
      }
    })
  },

  /** 清除乐观段视图标记（ack 对账成功后调用；消息已被 API 权威重建） */
  clearSegmentView: (pipelineId) => {
    set((state) => {
      if (!(pipelineId in state.segmentViewByPipeline)) return state
      const next = { ...state.segmentViewByPipeline }
      delete next[pipelineId]
      return { segmentViewByPipeline: next }
    })
  },

  /** 开始流式传输，记录正在流式传输的消息 ID */
  startStreaming: (pipelineId: string, messageId: string) => {
    set((state) => ({
      streamingState: {
        ...state.streamingState,
        [pipelineId]: {
          isStreaming: true,
          messageId,
          startedAt: Date.now(),
        },
      },
    }))
    // 同步 context key：有任意管道运行中 → pipeline.running=true（ADR §3.4）
    useContextKeys.getState().setPipelineRunning(true)
  },

  /** 停止流式传输，同时将 assistant 消息状态标记为 interrupted（与 DB 一致：
   *  内核停止后半截消息落库 status="interrupted"，前端不显示"已完成"终态） */
  stopStreaming: (pipelineId: string) => {
    set((state) => {
      const streamStatus = state.streamingState[pipelineId]
      const newStreamingState = { ...state.streamingState }
      delete newStreamingState[pipelineId]

      if (streamStatus?.messageId) {
        const pipelineMessages = state.messagesByPipeline[pipelineId] || []
        const messageIndex = pipelineMessages.findIndex(
          (m) => m.id === streamStatus.messageId,
        )

        if (messageIndex >= 0) {
          const updatedMessages = [...pipelineMessages]
          const stoppedMsg = updatedMessages[messageIndex]
          // 只收尾「流式 assistant」：sending/failed 的 user 乐观消息不归本方法管
          // （发送失败标 failed 后 stopStreaming 不得把它覆盖成 completed——
          // 单一消息数组：失败语义由调用方显式设置）。
          if (stoppedMsg.role === 'assistant') {
            // 收尾 part 状态：仅设置 message.status='interrupted' 会让 isStreamingMessage
            // 仍因残留的 'streaming'/'calling' part 返回 true（数据不一致，Stop 后再发新
            // 消息会卡在"思考中"）。与 handleStreamError 对齐：
            //   text/thinking 'streaming' -> 'done'
            //   tool_call 'calling'/'streaming' -> 'error'（abort 后未返回的工具调用视为失败）
            const finalizedParts = (stoppedMsg.parts || []).map((p) => {
              const partState = (p as { state?: string }).state
              if ((p.type === 'text' || p.type === 'thinking') && partState === 'streaming') {
                return { ...p, state: 'done' as const } as MessagePart
              }
              if (p.type === 'tool_call' && (partState === 'calling' || partState === 'streaming')) {
                return { ...p, state: 'error' as const } as MessagePart
              }
              return p
            })
            updatedMessages[messageIndex] = {
              ...stoppedMsg,
              parts: finalizedParts,
              status: 'interrupted',
              _lastUpdated: Date.now(),
            }

            return {
              streamingState: newStreamingState,
              messagesByPipeline: {
                ...state.messagesByPipeline,
                [pipelineId]: updatedMessages,
              },
            }
          }
        }
      }

      return {
        streamingState: newStreamingState,
      }
    })
    // 同步 context key：无管道运行中时回 idle（ADR §3.4）
    const stillRunning = Object.keys(get().streamingState).length > 0
    useContextKeys.getState().setPipelineRunning(stillRunning)
  },

  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => {
    return get().streamingState[pipelineId]?.isStreaming ?? false
  },

  /** 标记/取消「压缩中」（compression_started/ended 事件驱动） */
  setCompressing: (pipelineId: string, compressing: boolean) => {
    if (!pipelineId) return
    set((state) => {
      if (!!state.compressingByPipeline[pipelineId] === compressing) return state
      const next = { ...state.compressingByPipeline }
      if (compressing) {
        next[pipelineId] = true
      } else {
        delete next[pipelineId]
      }
      return { compressingByPipeline: next }
    })
  },

  /** 查询「压缩中」标记 */
  isCompressing: (pipelineId: string) => {
    return get().compressingByPipeline[pipelineId] ?? false
  },

  /** 轮次收尾队列深度快照（pipeline_round_finished.queue_depth） */
  setRoundQueueDepth: (pipelineId: string, depth: number) => {
    if (!pipelineId) return
    set((state) => ({
      roundQueueDepthByPipeline: {
        ...state.roundQueueDepthByPipeline,
        [pipelineId]: depth > 0 ? depth : 0,
      },
    }))
  },

  /** 停止受理中标记：点停止即置位，run 级终止事件/新 stream_start 清除 */
  setStopping: (pipelineId: string, stopping: boolean) => {
    if (!pipelineId) return
    set((state) => {
      if (!!state.stoppingByPipeline[pipelineId] === stopping) return state
      const next = { ...state.stoppingByPipeline }
      if (stopping) {
        next[pipelineId] = true
      } else {
        delete next[pipelineId]
      }
      return { stoppingByPipeline: next }
    })
  },

  /** 标记/清除指定管道的消息加载失败态（X1-F1）。设置点=调用方通知策略
   *  （sessionListStore 会话切换失败分支）；清除点统一收敛在 loadPipelineMessages
   *  成功路径——重试成功 / 下次进入 / WS 重连对账成功共用，不另起定时器。 */
  setLoadError: (pipelineId: string, failed: boolean) => {
    if (!pipelineId) return
    set((state) => {
      if (!!state.loadErrorByPipeline[pipelineId] === failed) return state
      const next = { ...state.loadErrorByPipeline }
      if (failed) {
        next[pipelineId] = true
      } else {
        delete next[pipelineId]
      }
      return { loadErrorByPipeline: next }
    })
  },

  /** 冷启动：从 API 写入最新消息并设置双游标 合并策略 — API 权威替换本地缓存；未被 API 覆盖的「飞行中」本页消息（乐观 user / 流式占位 / 刚完成回复）在新鲜度窗口内保留。 */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      // 刷新语义：API 权威替换本地缓存。例外——快照发起早于本页消息活动时
      // （后台对账 fetch 与发送/流式收尾竞态：快照查询在消息落库前执行、响应
      // 在气泡出现后到达），快照天然不含这些消息，直接替换会把刚出现的气泡
      // 抹掉且无后续拉取补回（reconciled 已置 true、增量补漏仅重连触发）。
      // 乐观 user 与流式占位/刚完成回复都在主数组（单一消息数组协议），
      // 由本处新鲜度窗口统一保护：
      //   - 乐观 user：带 cmid 未被覆盖 + timestamp 在窗口内
      //   - assistant：_lastUpdated 在窗口内（流式占位与刚完成回复，流式路径必打戳）
      // 被 isCoveredByApi 命中（id/cmid/recordId）的让位 API 权威版——不并存、
      // 不重复；空白占位（无内容无 parts 且非流式）与超窗残留照丢，保持刷新
      // 去漂移语义。
      const apiIds = new Set(sorted.map((m) => m.id))
      const apiByClientId = new Map<string, Message>()
      const apiByRecordId = new Map<string, Message>()
      for (const m of sorted) {
        if (m.clientMessageId) apiByClientId.set(m.clientMessageId, m)
        if (m.recordId) apiByRecordId.set(m.recordId, m)
      }
      const now = Date.now()
      const isFresh = (ts: number | undefined) =>
        typeof ts === 'number' && now - ts <= INFLIGHT_FRESH_MS
      const inflight = filterBlankMessages(
        (existing || []).filter((m) => {
          if (isCoveredByApi(m, apiIds, apiByClientId, apiByRecordId)) return false
          if (m.role === 'user' && m.clientMessageId && isFresh(new Date(m.timestamp).getTime())) {
            return true
          }
          return m.role === 'assistant' && isFresh(m._lastUpdated)
        }),
      )

      // API 权威基底（空白过滤 + 内存封顶；游标只按它计算）
      const apiFinal = capMessagesForMemory(filterBlankMessages(sorted))
      const finalMessages = inflight.length > 0
        ? capMessagesForMemory(mergeSorted(apiFinal, [...inflight].sort(compareMessages)))
        : apiFinal

      // 游标只按 API 权威消息计算（appendMessages 已对齐同口径）：飞行消息的
      // 本地 seq/timestamp 不可信，不得污染 after_sequence 补漏窗口。
      const topCursor = apiFinal.length > 0 ? (apiFinal[0].sequence ?? 0) : 0
      const bottomCursor = calculateBottomCursor(apiFinal, state.bottomCursorsByPipeline[pipelineId])

      logger.info('[initFromAPI] done: pipelineId=%s finalMsgs=%d inflightKept=%d (API 权威替换，保留飞行中)',
        pipelineId?.slice(0, 12), finalMessages.length, inflight.length)

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMessages,
        },
        topCursorsByPipeline: {
          ...state.topCursorsByPipeline,
          [pipelineId]: topCursor,
        },
        bottomCursorsByPipeline: {
          ...state.bottomCursorsByPipeline,
          [pipelineId]: bottomCursor,
        },
        hasMoreOlderByPipeline: {
          ...state.hasMoreOlderByPipeline,
          // 后端始终返回 has_more，前端直接使用
          [pipelineId]: hasMoreOlder ?? false,
        },
      }
    })
  },

  /** 向上翻页：将更早消息插入头部并更新 topCursor */
  prependMessages: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      if (messages.length === 0) {
        return {
          hasMoreOlderByPipeline: {
            ...state.hasMoreOlderByPipeline,
            [pipelineId]: false,
          },
          isLoadingOlderByPipeline: {
            ...state.isLoadingOlderByPipeline,
            [pipelineId]: false,
          },
        }
      }
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      // 合并新旧消息（保留已有 + 去重新增），不做 mergePreservingStreaming 二次合并——
      // 它内部的 mergeConsecutiveAssistantMessages 会把跨页边界的连续 assistant
      // 合并成一条，子管道（全是连续 tool 调用）50+50=100 条会被合并成 1-2 条，
      // 导致向上加载后消息几乎全丢。连续 assistant 的气泡合并由渲染层负责，
      // 数据层保持原始消息、sequence 连续。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
      // 过滤空白 assistant 消息（无 content 无 parts 无 toolCalls），避免空气泡
      const blankFiltered = filterBlankMessages(merged)
      // 内存封顶（与 append 同窗口，仅极端增长兜底）：cap 与翻页闸解耦——
      // 触发裁顶不再置 hasMoreOlder=false（2026-10-13 用户裁定：数据层截断
      // + 关闸让长会话滚到顶看不到更早历史，内核侧全量完好）。翻页与否唯
      // 一依据后端 has_more；cap 极端边界下继续翻页拉回的更早消息会被立即
      // 裁掉、桶不增长，链式续载的"条数不增长不续载"守卫兜住空转。
      const finalMerged = capMessagesForMemory(blankFiltered)
      const topCursor = finalMerged[0]?.sequence ?? 0
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMerged,
        },
        topCursorsByPipeline: {
          ...state.topCursorsByPipeline,
          [pipelineId]: topCursor,
        },
        hasMoreOlderByPipeline: {
          ...state.hasMoreOlderByPipeline,
          // 后端 has_more 唯一真值（cap 裁顶不再干预）
          [pipelineId]: hasMoreOlder ?? false,
        },
        isLoadingOlderByPipeline: {
          ...state.isLoadingOlderByPipeline,
          [pipelineId]: false,
        },
      }
    })
  },

  /** 断线补漏：追加缺失消息到底部并更新 bottomCursor */
  appendMessages: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      if (messages.length === 0) return state
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      // 含 clientMessageId/recordId 双键对账：
      // user 乐观版（UUID id）与 API 权威版（record_id）同键时丢弃本地乐观版，
      // 避免切会话回来两条 user 并存。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
      // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
      const finalMerged = capMessagesForMemory(merged)
      // 游标只按 API 权威消息计算（对齐 initFromAPI 口径）：
      // 本地乐观/流式占位的 seq 不进游标，after_sequence 补漏不跳空。
      const apiFinal = capMessagesForMemory(filterBlankMessages(sorted))
      const bottomCursor = calculateBottomCursor(apiFinal, state.bottomCursorsByPipeline[pipelineId])
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMerged,
        },
        bottomCursorsByPipeline: {
          ...state.bottomCursorsByPipeline,
          [pipelineId]: bottomCursor,
        },
      }
    })
  },

  /** 获取指定管道的顶部游标 */
  getTopCursor: (pipelineId: string) => {
    return get().topCursorsByPipeline[pipelineId] ?? 0
  },

  /** 获取指定管道的底部游标 */
  getBottomCursor: (pipelineId: string) => {
    return get().bottomCursorsByPipeline[pipelineId] ?? 0
  },

  /** 判断指定管道是否已成功加载过消息（可走增量补漏而非全量）。 权威定义：bottomCursor>0（已确认最大 sequence）且已有 >1 条消息。 */
  isInitialized: (pipelineId: string) => {
    const state = get()
    const count = (state.messagesByPipeline[pipelineId] || []).length
    const bottomCursor = state.bottomCursorsByPipeline[pipelineId] ?? 0
    return bottomCursor > 0 && count > 1
  },

  /** 判断指定管道是否还有更早的消息 */
  hasMoreOlder: (pipelineId: string) => {
    return get().hasMoreOlderByPipeline[pipelineId] ?? false
  },

  /** 按 pipelineId 从 API 拉取消息（init/翻页/补漏三种游标模式） */
  fetchMessages: async (
    pipelineId: string,
    options?: {
      limit?: number
      before_sequence?: number
      after_sequence?: number
      threadId?: string
      includeFullToolResults?: boolean
    },
  ) => {
    // 同类别新请求发起 = 旧请求作废：abort 在途旧请求并让出占位（同管道同
    // 类别任意时刻至多一个在途 fetch，取代即取消；跨类别不互取消）。
    const kind = fetchKindOf(options)
    const abortKey = abortKeyOf(pipelineId, kind)
    const previousController = _fetchAbortControllers.get(abortKey)
    if (previousController) {
      previousController.abort()
      _fetchAbortControllers.delete(abortKey)
      // 被取消的「加载更早」立即让出 loading 标记（新请求若同为翻页，下方会重新置位）
      if (get().isLoadingOlderByPipeline[pipelineId]) {
        set((s) => ({
          isLoadingOlderByPipeline: { ...s.isLoadingOlderByPipeline, [pipelineId]: false },
        }))
      }
    }

    const controller = new AbortController()
    _fetchAbortControllers.set(abortKey, controller)

    // 首屏全量（无游标）→ 置首载标记（消息区渲染骨架而非空态：空 ≠ 加载，OBS-R258-1）
    const noCursor = options?.before_sequence === undefined && options?.after_sequence === undefined

    const fetchPromise = (async () => {
      // 加载更早消息时设置 loading 状态（显示加载指示器）
      if (options?.before_sequence !== undefined) {
        set((state) => ({
          isLoadingOlderByPipeline: {
            ...state.isLoadingOlderByPipeline,
            [pipelineId]: true,
          },
        }))
      }
      if (noCursor) setInitialLoadingFlag(set, get, pipelineId, true)
      try {
        const limit = options?.limit ?? 50
        // 自动从 pipelineSessionMap 查找 sessionId 作为 threadId fallback
        // FEATURE-pipeline_unify: 统一传 pipelineRunId（主/子管道都用 pipelineId），
        // 后端统一走 pipelineRunId 路径，不再区分主/子。
        const sessionFallback = get().pipelineSessionMap[pipelineId]
        // 内部 API 不需要 threadId 三层降级：优先传参，其次 pipelineSessionMap，
        // 二者都无说明调用链有问题，直接报错
        const threadId = options?.threadId || sessionFallback
        if (!threadId) {
          logger.error('[pipelineMessageStore.fetchMessages] 无法确定 threadId: pipelineId=%s', pipelineId)
          throw new Error(`无法确定 threadId，pipelineId: ${pipelineId}`)
        }
        // 内部 API 不做内置重试，429/5xx 由 axios interceptor 统一处理
        const apiResult = await apiGetMessages(threadId, {
          limit,
          before_sequence: options?.before_sequence,
          after_sequence: options?.after_sequence,
          pipelineRunId: pipelineId,
          includeFullToolResults: options?.includeFullToolResults,
        })

        // 等待响应期间被同管道新请求 abort：本请求已作废，静默返回不写状态。
        // 旧响应的快照/游标相对新请求已过期，写入会让旧数据回滚新权威状态；
        // 取消是预期语义而非故障，不上抛（上层把失败一律当故障通知用户）。
        if (controller.signal.aborted) return

        const rawMessages: Message[] = apiResult.messages || []
        const mainMessages = rawMessages

        if (options?.after_sequence !== undefined) {
          get().appendMessages(pipelineId, mainMessages)
        } else if (options?.before_sequence !== undefined) {
          const hasMoreOlder = apiResult.has_more
          get().prependMessages(pipelineId, mainMessages, hasMoreOlder)
        } else {
          // 首次冷启动：从 API 响应读取 has_more，避免首次返回 <50 条时被错误地标记为无更多历史消息
          const hasMoreOlder = apiResult.has_more
          get().initFromAPI(pipelineId, mainMessages, hasMoreOlder)
          // 存活中间态重建流式占位（ADR 2026-08-27 §2.6 前端刷新恢复）：F5 后
          // 从后端内存寄存器恢复未落库的流式中间态（接口返回它 = 该消息尚未
          // 落库、流式进行中）。initFromAPI 权威替换后追加——占位走 addMessage
          // （单一消息数组协议），不推进 bottomCursor（与 ensureStreamingPlaceholder
          // 同规则；权威 seq 由 stream_end final_sequence / new_message 对账纠正）。
          const transientStates = apiResult.transient_states
          if (transientStates && transientStates.length > 0) {
            // 幂等双检：API 原始列表已含同 id 权威消息（落库完成，含空白过滤
            // 后不在 store 的边界）→ 跳过；本地已存在（WS 先到/INFLIGHT 保留）
            // → 跳过，绝不覆盖权威版或双插。
            const apiIds = new Set(mainMessages.map((m) => m.id))
            const existingIds = new Set(
              (get().getMessages(pipelineId) || []).map((m) => m.id),
            )
            for (const entry of transientStates) {
              const placeholder = buildTransientPlaceholder(entry, threadId)
              if (!placeholder) continue
              if (apiIds.has(placeholder.id) || existingIds.has(placeholder.id)) continue
              get().addMessage(pipelineId, placeholder)
            }
          }
        }
      } catch (err: any) {
        // abort 收场（含底层请求因取消而失败）：取消是预期语义而非故障，
        // 静默返回——不走失败日志、不上抛（上层把失败一律当故障通知用户）
        if (controller.signal.aborted) return
        const status = err?.response?.status ?? err?.status
        if (status === 404) {
          logger.debug('[pipelineMessageStore.fetchMessages] 管道消息暂不可用 (404): pipelineId=%s', pipelineId)
        } else {
          // 提取可读错误标识，避免 AxiosError 对象被 %s 序列化成 [object Object]。
          // 优先级：HTTP 状态码 > axios error code（ECONNABORTED/ERR_NETWORK 等）> message
          const errInfo = status || err?.code || err?.message || String(err)
          logger.warn(
            '[pipelineMessageStore.fetchMessages] 加载失败（已重试）: pipelineId=%s err=%s',
            pipelineId, errInfo,
          )
        }
        // 重新抛出，让上层调用方（loadPipelineMessages）能感知失败并决定通知策略。
        // 不在此处吞异常，否则所有调用方的 catch/then 分支永远拿不到错误。
        throw err
      } finally {
        // 身份守卫：被取代的请求不清理取代者的令牌、不动其 loading 标记（被取消者
        // 已在取代点复位）。落定双清（成功/失败皆清——「加载更早」残留卡翻页；
        // 首载残留卡骨架，失败态走 loadErrorByPipeline）
        if (_fetchAbortControllers.get(abortKey) === controller) {
          _fetchAbortControllers.delete(pipelineId)
          if (options?.before_sequence !== undefined && get().isLoadingOlderByPipeline[pipelineId]) {
            set({
              isLoadingOlderByPipeline: { ...get().isLoadingOlderByPipeline, [pipelineId]: false },
            })
          }
          if (noCursor) setInitialLoadingFlag(set, get, pipelineId, false)
        }
      }
    })()

    return fetchPromise
  },

  /** 加载管道消息的统一入口。收敛所有加载场景的流式保护 + 双游标决策。 详见接口声明处的注释。 */
  loadPipelineMessages: async (pipelineId, options) => {
    const { threadId, mode = 'auto', skipStreamingCheck = false } = options
    // 刷新后首屏：等 IndexedDB rehydrate 完成再决策——不等待时本地缓存不可见，
    // auto 会误判"未对账"走全量 init，缓存恢复路径被跳过。
    // init（显式强制全量）不需要缓存，跳过等待直接请求。
    if (mode !== 'init') await waitForMessageHydration()
    const state = get()
    const existingCount = (state.messagesByPipeline[pipelineId] || []).length

    // 流式保护：流式输出中且已有实质消息时跳过所有 API 调用。
    if (!skipStreamingCheck && state.isStreaming(pipelineId) && existingCount > 1) {
      return { ok: true }
    }

    // 段清单随会话/管道打开刷新（非仅首次）：新段由重新生成/回退/编辑重发产生
    // （内核把这些动作的被换后缀冻结为段），而这些动作都在跑一轮对话——轮次结束
    // 事件会触发清单刷新（useRealtimeEvents）。此处若只在「无条目时」拉一次，
    // 打开会话时拿到空清单（[]）后该条目永久存在，「有条目即跳过」的判定会把
    // 刷新永久挡住，‹i/n› 切换器在会话生命周期内永远不出现（用户实证：重试
    // 多次仍找不到分支切换按钮）。非阻塞、不参与游标与模式决策，失败降级见
    // fetchSegments。
    void state.fetchSegments(pipelineId)

    // 模式决策：
    // - mode='init'：页面刷新 / 显式强制 → 全量 initFromAPI，丢弃本地，以 API 权威重建
    // - mode='auto'：切换会话 → 该管道尚未对账（刷新后首次进入 / 无缓存）：全量对账一次；
    //   已对账：不做任何 API 调用，直接用缓存
    // - mode='backfill'：WS 重连补漏 → 增量追加
    // 对账标记的信任条件：rehydrate 后 reconciledByPipeline 被重置为 {}，
    // 但 IndexedDB 缓存里的消息本体仍有效（persist 快照只含已确认消息）。刷新后首次
    // auto 进入：本地有缓存 → 页面立即用缓存渲染（秒开），同时后台静默全量对账
    // （修正断线空洞/残影，见 reconcileFromAPI）；仅缓存缺失/为空时才同步全量重建。
    const reconciled = state.reconciledByPipeline[pipelineId] ?? false
    const hasLocalMessages = existingCount > 0
    const isInit = mode === 'init' || (mode === 'auto' && !reconciled && !hasLocalMessages)
    const needReconcile = mode === 'auto' && !reconciled && hasLocalMessages
    const needBackfill = mode === 'backfill'

    try {
      if (isInit) {
        await state.fetchMessages(pipelineId, { threadId })
        set((s) => ({ reconciledByPipeline: { ...s.reconciledByPipeline, [pipelineId]: true } }))
      } else if (needReconcile) {
        // 刷新后缓存秒开 + 后台静默全量对账（不等结果，修正空洞/残影）
        void reconcileFromAPI(pipelineId, threadId)
      } else if (needBackfill) {
        const bottomCursor = state.bottomCursorsByPipeline[pipelineId] ?? 0
        await state.fetchMessages(pipelineId, { threadId, after_sequence: bottomCursor })
      }
      // mode='auto' 且已对账 或 缓存为空 → 不做 API 调用，直接用缓存
      // 加载成功即清该管道失败态（X1-F1）：重试/下次进入/WS 重连对账成功的
      // 共用清除点，失败态随现有加载流自然消失，不另起定时器。
      if (get().loadErrorByPipeline[pipelineId]) {
        get().setLoadError(pipelineId, false)
      }
      return { ok: true }
    } catch (error) {
      return { ok: false, error }
    }
  },

  // Parts 统一修改方法

  /** 追加一个新 Part 到指定消息 */
  appendPart: (pipelineId: string, messageId: string, part: MessagePart) => {
    set((state) => mutateMessageParts(state, pipelineId, messageId, null, (parts) => [...parts, part]))
  },

  /** 更新指定消息的某个 Part（按 partIndex 精确定位） */
  updatePart: (pipelineId: string, messageId: string, partIndex: number, updates: Partial<MessagePart>) => {
    set((state) =>
      mutateMessageParts(state, pipelineId, messageId, partIndex, (parts) => {
        const updatedParts = [...parts]
        updatedParts[partIndex] = { ...updatedParts[partIndex], ...updates } as MessagePart
        return updatedParts
      }),
    )
  },

  /** 向指定 Part 追加文本内容（用于流式增量） */
  appendToPart: (pipelineId: string, messageId: string, partIndex: number, content: string) => {
    set((state) =>
      mutateMessageParts(state, pipelineId, messageId, partIndex, (parts) => {
        const part = parts[partIndex]
        // 只有 text 和 thinking 类型支持追加
        if (part.type !== 'text' && part.type !== 'thinking') return null
        return [
          ...parts.slice(0, partIndex),
          {
            ...part,
            content: (part as { content: string }).content + content,
          } as MessagePart,
          ...parts.slice(partIndex + 1),
        ]
      }),
    )
  },

  /** 结束消息流式状态：所有 Part.state = 'done', 消息 status = 'completed' */
  finalizeMessage: (pipelineId: string, messageId: string) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId]
      if (!pipelineMessages) return state
      const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (msgIndex < 0) return state
      const msg = pipelineMessages[msgIndex]
      const parts = msg.parts || []
      const finalizedParts = parts.map((p) => {
        if (p.type === 'text' || p.type === 'thinking') {
          return { ...p, state: 'done' as const } as MessagePart
        }
        if (p.type === 'tool_call') {
          return {
            ...p,
            state: (p.state === 'error' ? 'error' : 'done') as ('done' | 'error'),
          } as MessagePart
        }
        return p
      })
      const updatedMessages = [...pipelineMessages]
      updatedMessages[msgIndex] = {
        ...msg,
        parts: finalizedParts,
        status: 'completed',
        _lastUpdated: Date.now(),
      }
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 获取指定消息中最后一个指定类型的 Part 的 index */
  findLastPartIndex: (pipelineId: string, messageId: string, type: MessagePart['type']) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    for (let i = msg.parts.length - 1; i >= 0; i--) {
      if (msg.parts[i].type === type) return i
    }
    return -1
  },

  /** 获取指定消息中 state='streaming' 的最后一个 text Part 的 index */
  findStreamingPartIndex: (pipelineId: string, messageId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    for (let i = msg.parts.length - 1; i >= 0; i--) {
      const p = msg.parts[i]
      // 仅匹配 text part：正文 stream_chunk 只应追加到 text part。
      // 若匹配 thinking part，后端 </think> 关闭时先发正文 chunk（thinking_end
      // 在下一次 delta 才发）会导致正文被吞进 thinking part，正文不显示或
      // 等 thinking_end 后才一次性渲染。thinking 的流式追加由 thinkingHandler
      // 用 findLastPartIndex(type='thinking') 精确路由，不走本方法。
      if (p.type === 'text' && p.state === 'streaming') return i
    }
    return -1
  },

  /** 获取指定消息中指定 callId 的 tool_call Part 的 index */
  findToolCallPartIndex: (pipelineId: string, messageId: string, callId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    return msg.parts.findIndex((p) => p.type === 'tool_call' && (p as ToolCallPart).callId === callId)
  },
}),
  // 持久化策略（迁移到 IndexedDB 后）：
  // - 单管道最多 250 条（PERSIST_MAX_MESSAGES_PER_PIPELINE）
  // - 全局总体积上限 100 MB（PERSIST_MAX_TOTAL_BYTES），超限按 LRU 淘汰最不活跃管道
  // - 无 TTL：被淘汰或缺失的管道由 API 冷启动重新加载覆盖
  {
    name: 'pipeline-messages',
    version: 1,
    // IndexedDB storage：GB 级容量、异步不阻塞 UI；不可用时自动降级内存（见 indexedDbStorage）
    storage: indexedDbStorage,
    // 仅持久化核心数据，排除运行时状态。
    // 先按条数 + 总体积 LRU 裁剪出保留的管道集合，再统一应用于消息/元数据/游标，保证一致性。
    partialize: buildPersistedSnapshot,
    // 恢复时合并默认状态（运行时状态用默认值）
    merge: (persisted, current) => {
      const p = (persisted as Partial<PipelineMessageState>) || {}
      // 复活链已废除：rehydrate 一律丢弃 streaming/占位消息——
      // 刷新后的飞行中内容由两条成熟机制恢复：WS 重连 replay（last_sequence
      // watermark 回放事件流，前端按精确 ID 重建）+ 完成后 backfill（API 权威
      // 记录）。持久化快照只保留已确认消息，杜绝「刷新后本地残影复活」通道。
      const cleanedMessages: Record<string, Message[]> = {}
      if (p.messagesByPipeline) {
        for (const [pid, msgs] of Object.entries(p.messagesByPipeline)) {
          if (!msgs) continue
          cleanedMessages[pid] = msgs.filter((m) => m.status !== 'streaming')
        }
      }
      return {
        ...current,
        ...p,
        messagesByPipeline: cleanedMessages,
        // 运行时状态强制重置（不信任持久化值）
        streamingState: {},
        compressingByPipeline: {},
        isLoadingOlderByPipeline: {},
        // 重置对账标记：应用重启后持久化的 bottomCursor 不可信（可能来自流式断线时的乐观值），
        // 所有 pipeline 必须重新全量对账，避免已加载区间内的空洞无法通过增量补漏修复。
        reconciledByPipeline: {},
      }
    },
    // 迁移配套：消息缓存已迁 IndexedDB，旧 localStorage['pipeline-messages'] 不再读取。
    // rehydrate 完成后一次性清理旧 key，释放 localStorage 空间给 agentTabs 等使用。
    onRehydrateStorage: () => () => {
      try {
        if (window.localStorage.getItem('pipeline-messages') !== null) {
          window.localStorage.removeItem('pipeline-messages')
        }
      } catch {
        // localStorage 不可用时忽略，不影响 rehydrate
      }
      // 唤醒 waitForMessageHydration 的等待者（rehydrate 完成 = 缓存可见，
      // loadPipelineMessages 的 auto 决策不再误判）
      markMessageHydrated()
    },
  },
))
