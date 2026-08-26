/** 流式事件处理器公共工具函数 统一抽取的消息 ID 提取、流式占位符创建、Streaming 状态管理， */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

/** 合并本地流式累积的 parts 与后端 stream_end/new_message 下发的 serverParts。
 *
 * 两个调用方语义不同：
 * - `new_message`（preferServer=true）：data.message 是**落库权威完整形态**
 *   （经共享 mapper 还原，含全部轮次的 thinking/text/tool_call）。以 server
 *   为基底，本地只补充 server 缺失的增量（tool_result 结果注入、本地独有
 *   text/thinking）。绝不丢弃 server 权威文本——否则「工具卡片在、最终文本
 *   消失」（本地有 tool_call 即视为有内容、server 文本被整体丢弃）。
 * - `stream_end`（preferServer=false，默认）：parts 是**末轮快照**（可能只有
 *   tool_call 或只有 text，残缺）。本地已按真实时序累积完整 parts
 *   （text→tool_call→text）时以本地为基底，server 快照只补充本地缺失的
 *   权威增量（断线期间丢失的 chunk）。
 *
 * 两种模式都做：残留 streaming 态收敛为 done + tool_call 结果增量回填。
 */
export function mergeStreamingParts(
  localParts: any[] | undefined,
  serverParts: any[] | undefined,
  serverFullContent?: string,
  localContent?: string,
  opts?: { preferServer?: boolean },
): { parts: any[]; content: string } {
  const local = localParts || []
  const server = serverParts || []
  const preferServer = opts?.preferServer ?? false

  // 收敛残留 state='streaming' 的 text/thinking part 为 'done'：stream_end 已标志
  // 流终止，残留的 streaming 状态通常来自 block_end 丢失/乱序，不收尾会让卡片
  // 图标一直转圈（参见 streamTimingRepro 场景3）。
  const finalize = (p: any) =>
    (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming'
      ? { ...p, state: 'done' as const }
      : p

  // part 是否有实质内容（text/thinking 有内容、tool_call/system 存在即算）。
  const hasSubstance = (p: any) =>
    (p.type === 'text' && p.content) ||
    (p.type === 'thinking' && p.content) ||
    p.type === 'tool_call' ||
    (p.type === 'system' && p.content)

  // 无 server parts（旧后端/未下发）→ 本地原样保留（仅收敛 streaming 态）。
  if (server.length === 0) {
    const needsFinalize = local.some(
      (p) => (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming',
    )
    const parts = needsFinalize ? local.map(finalize) : local
    const currentContent = localContent || ''
    const content =
      serverFullContent && serverFullContent.length > currentContent.length
        ? serverFullContent
        : currentContent
    return { parts, content }
  }

  // 默认模式（stream_end 残缺快照）且本地为空 → 直接用 server（保持引用）。
  if (!preferServer && local.length === 0) {
    const needsFinalize = server.some(
      (p) => (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming',
    )
    const parts = needsFinalize ? server.map(finalize) : server
    const currentContent = localContent || ''
    const content =
      serverFullContent && serverFullContent.length > currentContent.length
        ? serverFullContent
        : currentContent
    return { parts, content }
  }

  // 默认模式且本地全部无实质内容（空占位残留）→ 直接用 server（保持引用）。
  if (!preferServer && local.every((p: any) => !hasSubstance(p))) {
    const needsFinalize = server.some(
      (p) => (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming',
    )
    const parts = needsFinalize ? server.map(finalize) : server
    const currentContent = localContent || ''
    const content =
      serverFullContent && serverFullContent.length > currentContent.length
        ? serverFullContent
        : currentContent
    return { parts, content }
  }

  // 基底选择：preferServer（new_message 权威完整形态）→ server；
  // 否则（stream_end 残缺快照）→ 本地完整累积优先。
  const base = preferServer ? server : local
  const supplement = preferServer ? local : server

  // 收敛基底残留 streaming 态（无残留时保持原引用）。
  const needsFinalize = base.some(
    (p) => (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming',
  )
  const baseParts = needsFinalize ? base.map(finalize) : base

  // 增量补充（supplement 中基底缺失的 text/thinking/tool_call，按内容指纹去重）。
  const baseTexts = new Set(
    baseParts
      .filter((p: any) => p.type === 'text' && p.content)
      .map((p: any) => p.content),
  )
  const baseThink = new Set(
    baseParts
      .filter((p: any) => p.type === 'thinking' && p.content)
      .map((p: any) => p.content),
  )
  const baseToolIds = new Set(
    baseParts.filter((p: any) => p.type === 'tool_call').map((p: any) => p.callId),
  )

  // tool_call 结果增量回填：supplement 中同名 tool_call 携带的
  // result/error/resultData/containerTaskId/durationMs/partialOutput，
  // 基底缺失时补入（不覆盖基底权威字段）。
  const hasToolEnrich = baseParts.some(
    (p: any) =>
      p.type === 'tool_call' &&
      supplement.some(
        (sp: any) =>
          sp.type === 'tool_call' &&
          sp.callId === p.callId &&
          (sp.result !== undefined ||
            sp.error !== undefined ||
            sp.resultData !== undefined ||
            sp.containerTaskId !== undefined ||
            sp.durationMs !== undefined ||
            sp.partialOutput !== undefined),
      ),
  )
  const enriched = hasToolEnrich
    ? baseParts.map((p: any) => {
        if (p.type !== 'tool_call') return p
        const supMatch = supplement.find(
          (sp: any) => sp.type === 'tool_call' && sp.callId === p.callId,
        )
        if (!supMatch) return p
        const merged: any = { ...p }
        for (const k of [
          'result', 'error', 'resultData', 'containerTaskId', 'durationMs', 'partialOutput',
        ]) {
          if (supMatch[k] !== undefined && merged[k] === undefined) {
            merged[k] = supMatch[k]
          }
        }
        return merged
      })
    : baseParts

  const extra: any[] = []
  for (const sp of supplement) {
    if (sp.type === 'text' && sp.content && !baseTexts.has(sp.content)) {
      extra.push(finalize(sp))
      baseTexts.add(sp.content)
    } else if (sp.type === 'thinking' && sp.content && !baseThink.has(sp.content)) {
      extra.push(finalize(sp))
      baseThink.add(sp.content)
    } else if (sp.type === 'tool_call' && !baseToolIds.has(sp.callId)) {
      // supplement 有基底未带的 tool_call（快照截断）→ 保留
      extra.push(finalize(sp))
      baseToolIds.add(sp.callId)
    }
  }

  const parts = extra.length > 0 ? [...enriched, ...extra] : enriched

  // content 校准：server 的 full_content 更长时采用（本地逐 chunk 拼接可能不完整）
  const currentContent = localContent || ''
  const content =
    serverFullContent && serverFullContent.length > currentContent.length
      ? serverFullContent
      : currentContent

  return { parts, content }
}

/** 从事件数据中提取消息 ID 统一处理 message_id 的多种来源，避免各 handler 重复写 */
export function extractMessageId(eventData: any): string | null {
  if (!eventData) return null
  return (
    eventData.message_id
    || eventData.data?.message_id
    || eventData.data?.ai_message_id
    || null
  )
}

/** 统一启动管道流式状态 pipelineStore.streamingState 是唯一数据源。 */
export function startPipelineStreaming(
  pipelineId: string,
  messageId: string,
): void {
  pipelineStore.getState().startStreaming(pipelineId, messageId)

  // 轮次级安全兜底：单个消息的流式若超过 90s 仍未结束（说明 stream_end/new_message
  // 事件漏接或与 pipeline 未对齐），强制收尾，避免 UI 永久卡在
  // "思考中"。**按 messageId 命中**：仅当当前 streamingState 仍是本条消息时才清理，
  // 因此不会误杀后续新轮次（新轮次 messageId 不同）。后端数据已持久化，强制收尾后
  // 内容仍可正常渲染/刷新恢复。
  const turnMsgId = messageId
  setTimeout(() => {
    const ps = pipelineStore.getState()
    const cur = (ps as any).streamingState?.[pipelineId]
    if (cur?.isStreaming && cur?.messageId === turnMsgId) {
      loggers.websocket.warn(
        '[STREAM-TIMEOUT] 单消息流式超过 90s 未结束，强制收尾防卡死: pipeline=%s msg=%s',
        pipelineId?.slice(0, 12), turnMsgId?.slice(0, 12),
      )
      ps.stopStreaming(pipelineId)
    }
  }, 90000)
}

/** 停止管道流式传输——只清事件明确归属的管道。
 *  「清别人状态」已废除：不再顺带 stopStreaming(threadId)——
 *  threadId 是会话坐标非管道 ID，拿它当管道清是"主管道 ID == sessionId"
 *  隐性等式的猜测，等式不成立时清不到任何东西、成立时纯属重复。 */
export function stopPipelineStreaming(pipelineId: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
}

// allocateNextSequence（本地拼 localMax+1）已废除：客户端
// 不再为 store 消息伪造 sequence——事件未携带权威 seq 时挂空（compareMessages
// 回落 timestamp 排序），权威值到达后由对账纠正。

/** 确保流式占位符消息存在 精确 ID 生命周期：占位气泡以后端
 * 真实 message_id 为键（stream_start 事件携带；发送瞬间的反馈由 pending 区承担，
 * 不再在主 store 建 placeholder_ 前缀气泡）。同 id 已存在（chunk 先于 start 自动
 * 建占位 / 事件重放）→ 原地保留，绝不改写别的消息的 id（旧"并入前一条改写 ID"
 * 是迟到事件劫持当前气泡的空气泡根因，已废除）。 */
export function ensureStreamingPlaceholder(
  pipelineId: string,
  messageId: string,
  threadId?: string,
  backendSequence?: number,
): void {
  startPipelineStreaming(pipelineId, messageId)

  const store = pipelineStore.getState()
  const existing = store.getMessages(pipelineId)

  // 精确匹配：同 id 消息已存在（含已积累的流式内容）→ 原地保留
  if (existing.some((m) => m.id === messageId)) return

  // orphan 清理：本轮 start 之前残留的 streaming assistant（上一轮 stream_end
  // 丢失/断线遗留）。有完整内容 → completed 保留；有未解析 tool_call 或完全
  // 无内容 → 移除（后端权威内容由对账补回）。
  for (const msg of existing) {
    if (msg.role === 'assistant' && msg.status === 'streaming' && msg.id !== messageId) {
      const parts = msg.parts || []
      const hasTextContent = (msg.content || '').length > 0
      const unresolvedToolCalls = parts.some(
        (p: any) => p.type === 'tool_call' && (p.state === 'calling' || p.state === 'streaming')
      )
      const resolvedParts = parts.filter(
        (p: any) => p.type !== 'tool_call' || (p.state !== 'calling' && p.state !== 'streaming')
      )

      if (unresolvedToolCalls) {
        store.removeMessage(pipelineId, msg.id)
      } else if (hasTextContent || resolvedParts.length > 0) {
        const finalizedParts = resolvedParts.map((p: any) => {
          if (p.type === 'tool_call') return { ...p, state: 'done' as const }
          if (p.type === 'text' || p.type === 'thinking') {
            return (p as any).state === 'streaming' ? { ...p, state: 'done' as const } : p
          }
          return p
        })
        store.updateMessage(pipelineId, msg.id, {
          status: 'completed',
          parts: finalizedParts.length > 0 ? finalizedParts : undefined,
        } as any)
      } else {
        store.removeMessage(pipelineId, msg.id)
      }
    }
  }

  // 前一条不是本消息（正常路径：前一条是 user/已完成的 assistant）→ 新建占位。
  // sequence：stream_start 事件不携带消息 seq（后端在引擎执行时才分配），
  // 挂 undefined（排序落到末尾按 timestamp），stream_end 的 final_sequence /
  // new_message 的权威 seq 到达后由对账纠正——绝不本地拼 localMax+1 冒充权威值。
  const placeholderSeq = backendSequence != null && backendSequence > 0
    ? backendSequence
    : undefined

  store.addMessage(pipelineId, {
    id: messageId,
    sessionId: threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: placeholderSeq,
    status: 'streaming',
    _lastUpdated: Date.now(),
  } as any)
}

/** 从事件数据中提取 threadId 统一处理 `eventData.data?._threadId || eventData._threadId` 模式。 */
export function extractThreadId(eventData: any): string | undefined {
  return eventData.data?._threadId || eventData._threadId
}

/** 终止管道：清理 streamingState 仅在 stream_end / stream_error 等终止事件到达时调用。
 *  只清事件明确归属的管道。 */
export function terminatePipeline(pipelineId: string): void {
  stopPipelineStreaming(pipelineId)
}

