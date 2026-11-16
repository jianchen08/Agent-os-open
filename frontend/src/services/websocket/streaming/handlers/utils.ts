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

// ── 停止已请求护栏 ──
// 用户点停（router.handleStopGenerate）后、内核取消链路收尾（round_finished/
// stream_error）前到达的迟到 chunk 会经 ensureStreamingPlaceholder →
// startPipelineStreaming 把生成态重新点亮（Stop 按钮复活、正文继续增长的
// 「点了没反应」体感）。护栏按 (pipelineId, messageId) 存续：同消息的迟到
// 事件仍压制生成态（内容照常追加，不做数据丢弃）；新一轮 stream_start
// （不同 message_id）到达即解除；TTL 兜底防泄漏。
const STOP_REQUEST_TTL_MS = 15_000
const stopRequestedByPipeline = new Map<string, { messageId: string; at: number }>()

/** 用户请求停止：登记护栏（router.handleStopGenerate 调用）。 */
export function markStopRequested(pipelineId: string, messageId: string | null): void {
  if (!pipelineId) return
  stopRequestedByPipeline.set(pipelineId, { messageId: messageId || '', at: Date.now() })
}

/** 查询护栏：该消息的迟到事件是否应压制生成态重点亮。TTL 过期自动失效。 */
export function isStopRequested(pipelineId: string, messageId?: string): boolean {
  const mark = stopRequestedByPipeline.get(pipelineId)
  if (!mark) return false
  if (Date.now() - mark.at > STOP_REQUEST_TTL_MS) {
    stopRequestedByPipeline.delete(pipelineId)
    return false
  }
  // 空 messageId 登记（点停时本地尚无流式消息）→ 压制该管道一切迟到事件；
  // 有 id → 仅压制同消息（新轮次不同 id 不受影响）。
  return !mark.messageId || !messageId || mark.messageId === messageId
}

/** 新一轮 stream_start：与护栏登记的不是同一消息 → 内核已在新轮次跑，解除护栏。 */
export function noteFreshStreamStart(pipelineId: string, messageId: string): void {
  const mark = stopRequestedByPipeline.get(pipelineId)
  if (!mark) return
  if (!mark.messageId || mark.messageId !== messageId) {
    stopRequestedByPipeline.delete(pipelineId)
  }
}

/** run 收尾（pipeline_round_finished / stream_error）：护栏使命完成，清除。 */
export function clearStopRequested(pipelineId: string): void {
  stopRequestedByPipeline.delete(pipelineId)
}

/** 启动管道流式传输（带停止护栏检查：护栏命中则只落数据不点亮生成态）。 */
export function startPipelineStreaming(
  pipelineId: string,
  messageId: string,
): void {
  if (!isStopRequested(pipelineId, messageId)) {
    pipelineStore.getState().startStreaming(pipelineId, messageId)
  }

  // 轮次级安全兜底：单个消息的流式若超过 90s 仍未结束（说明 stream_end/new_message
  // 事件漏接或与 pipeline 未对齐），强制收尾，避免 UI 永久卡在
  // "思考中"。**按 messageId 命中**：仅当当前 streamingState 仍是本条消息时才清理，
  // 因此不会误杀后续新轮次（新轮次 messageId 不同）。后端数据已持久化，强制收尾后
  // 内容仍可正常渲染/刷新恢复。
  // 工具执行轮间豁免：本条消息已随轮收尾 completed/被移除而生成态仍挂着时，
  // 说明 run 仍在跑（生成态由 pipeline_round_finished 终结）——只有消息还挂在
  // streaming 状态（chunk 断流真卡死）才强制收尾，不得掐断轮间生成态。
  const turnMsgId = messageId
  setTimeout(() => {
    const ps = pipelineStore.getState()
    const cur = (ps as any).streamingState?.[pipelineId]
    if (!(cur?.isStreaming && cur?.messageId === turnMsgId)) return
    const msg = ps.getMessages(pipelineId).find((m: any) => m.id === turnMsgId)
    if (!msg || msg.status !== 'streaming') {
      loggers.websocket.info(
        '[STREAM-TIMEOUT] 轮间生成态维持（消息已收尾，run 未结束）: pipeline=%s msg=%s',
        pipelineId?.slice(0, 12), turnMsgId?.slice(0, 12),
      )
      return
    }
    loggers.websocket.warn(
      '[STREAM-TIMEOUT] 单消息流式超过 90s 未结束，强制收尾防卡死: pipeline=%s msg=%s',
      pipelineId?.slice(0, 12), turnMsgId?.slice(0, 12),
    )
    ps.stopStreaming(pipelineId)
  }, 90000)
}

/** 停止管道流式传输——只清事件明确归属的管道。
 *  「清别人状态」已废除：不再顺带 stopStreaming(threadId)——
 *  threadId 是会话坐标非管道 ID，拿它当管道清是"主管道 ID == sessionId"
 *  隐性等式的猜测，等式不成立时清不到任何东西、成立时纯属重复。 */
export function stopPipelineStreaming(pipelineId: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
}

/** run 终态清扫：移除残留的空流式占位（无内容、无 parts）。
 *  空轮次占位（init 等无产出轮）在 run 仍在流式时被 stream_end 保留
 *  （「思考中」指示跨轮延续），run 终止后即成孤儿——run 级终止出口
 *  （pipeline_round_finished / run_failed）在 terminatePipeline 之前调此函数。
 *  必须先于 terminate：stopStreaming 会把 streamingState.messageId 命中的
 *  消息标 interrupted（渲染「生成已中断」假象），空占位先移除才不会被标。
 *  有内容/有 parts 的流式消息不在此列：断流残留由 stopStreaming 的
 *  interrupted 语义与 90s 兜底管辖，清扫不得越界。 */
export function sweepEmptyStreamingPlaceholders(pipelineId: string): void {
  const store = pipelineStore.getState()
  for (const msg of store.getMessages(pipelineId)) {
    if (
      msg.role === 'assistant' &&
      msg.status === 'streaming' &&
      (msg.content || '').length === 0 &&
      (msg.parts || []).length === 0
    ) {
      store.removeMessage(pipelineId, msg.id)
    }
  }
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
  // 丢失/断线遗留）。有完整内容或未解析 tool_call → completed 保留；完全无内容
  // → 移除（后端权威内容由对账补回）。
  //
  // 未解析 tool_call 的消息**绝不删除**（2026-10-29 跨轮残影根因）：工具事件
  // 契约 message_id「晚一轮」（见 toolHandler 归并注释），迟到 tool_result 要靠
  // 声明卡的 callId 归并回上一轮；消息被删后 findToolCallMessageId 找不到，
  // FIXUP 会把工具卡建到下一轮气泡里。保留正文与声明卡，未完成卡按中断语义
  // 标 error（迟到结果仍会覆盖为终态），跨轮归属不漂移。
  for (const msg of existing) {
    if (msg.role === 'assistant' && msg.status === 'streaming' && msg.id !== messageId) {
      const parts = msg.parts || []
      const hasTextContent = (msg.content || '').length > 0
      const finalizedParts = parts.map((p: any) => {
        if (p.type === 'tool_call') {
          const partState = (p as { state?: string }).state
          // 未解析卡（仍在飞）→ error：让位给下一轮即视为中断；迟到结果覆盖
          // 为终态。已解析卡保持原终态。
          if (partState === 'calling' || partState === 'streaming') {
            return { ...p, state: 'error' as const }
          }
          return p
        }
        if ((p.type === 'text' || p.type === 'thinking') && (p as any).state === 'streaming') {
          return { ...p, state: 'done' as const }
        }
        return p
      })

      if (hasTextContent || finalizedParts.length > 0) {
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

