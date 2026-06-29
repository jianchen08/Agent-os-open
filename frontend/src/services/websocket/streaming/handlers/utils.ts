/**
 * 流式事件处理器公共工具函数
 *
 * 统一抽取的消息 ID 提取、流式占位符创建、Streaming 状态管理，
 * 消除各 handler 中的重复代码，确保 pipeline_id 唯一路由原则。
 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

/**
 * 判断 pipeline 是否"被关注"——前端是否需要处理它的流式事件。
 *
 * BUG-FIX-fix_20260628_inactive_pipeline_amplification:
 * 问题根因: 全局单连接模式下，后端并发任务（多个 L3 子任务同时跑）时，所有 pipeline
 *   的流式事件（chunk/thinking/start/end）都涌进同一个 WS 连接。前端 handler 无条件
 *   为每个事件创建占位符 + 写 store，即使该 pipeline 用户根本没开标签页。
 *   e2e 实测：60 秒内 1000+ 个别人 pipeline 的事件为幽灵管道创建 store 条目，
 *   触发大量无意义的 zustand set / persist / 重渲染，挤占主线程，用户自己的消息
 *   延迟 4-5 秒甚至更久才响应。
 * 修复方案: 流式事件入口（chunk/start 等）先判断 pipeline 是否被关注，不被关注的
 *   直接丢弃，不写 store、不进 RAF 缓冲。关注判据（任一满足）：
 *   1. 是当前 activePipelineId（用户正在看的管道）
 *   2. 已在 pipelineStore.pipelines 注册（用户曾交互，或会话切换时预注册）
 *   3. 在 agentTabStore.tabs 中有对应标签页（用户打开过的子管道）
 *   终止事件（stream_end/error/state_change）不过滤——它们是轻量的，且必须清理
 *   可能残留的 streamingState，过滤会导致状态泄漏。
 * 影响范围: 多 pipeline 并发时的前端主线程占用与渲染压力
 * 修复日期: 2026-06-28
 */
export function isPipelineRelevant(pipelineId: string): boolean {
  if (!pipelineId) return false
  const state = pipelineStore.getState()
  // 判据 1: 当前活跃管道
  if (state.activePipelineId === pipelineId) return true
  // 判据 2: 已注册的管道（用户交互过/会话切换预注册过）
  if (state.pipelines[pipelineId]) return true
  // 判据 3: 用户打开的标签页对应的管道
  const tabs = useAgentTabStore.getState().tabs
  return tabs.some((t) => t.pipelineRunId === pipelineId)
}

/**
 * 合并本地流式累积的 parts 与后端 stream_end/new_message 下发的 serverParts。
 *
 * BUG-FIX-fix_20260624_stream_overwrite_regression:
 * 问题根因: 前一版（fix_20260617_stream_end_overwrite / fix_20260617_new_msg_overwrite）
 *   用「parts 数组长度」决定保留本地还是 server：localParts.length > serverParts.length
 *   ? localParts : serverParts。但后端 _build_parts_from_state 从 state.raw_thinking /
 *   raw_tool_calls / raw_result 构建 parts，而 state 这些字段在每轮 LLM 迭代中被覆盖，
 *   所以 serverParts 只反映「最后一轮」的内容，且其数量与轮次数毫无关系。
 *   一旦 serverParts 数量 ≥ 本地累积的多轮 parts，本地完整的流式内容（前几轮思考、
 *   工具调用）就被末轮残缺的 serverParts 整体覆盖 —— 用户表现为「流式输出过程中
 *   已经显示的内容突然变了/消失了」。
 *
 * 正确语义: 本地 parts 由 thinking_start/chunk、stream_chunk、tool_call 等事件
 *   逐个 append 累积，是逐事件的完整真相；serverParts 是末轮残缺快照。因此
 *   本地有实质内容时必须优先保留本地，serverParts 仅作兜底。
 *
 * 兜底场景: 本地 parts 为空或全部无内容（极端情况：所有流式事件丢失、纯 new_message
 *   注入的消息），此时 serverParts 是唯一内容来源。
 *
 * content 校准: server 的 full_content（来自 state.raw_result，最终完整文本）是可靠的，
 *   本地 content 是逐 chunk 拼接的。若 server 的更长，说明本地拼接有缺失，用它校准。
 *
 * @returns 合并后的 { parts, content }
 */
export function mergeStreamingParts(
  localParts: any[] | undefined,
  serverParts: any[] | undefined,
  serverFullContent?: string,
  localContent?: string,
): { parts: any[]; content: string } {
  const hasLocalContent =
    !!localParts &&
    localParts.length > 0 &&
    localParts.some(
      (p) =>
        (p.type === 'text' && p.content) ||
        (p.type === 'thinking' && p.content) ||
        p.type === 'tool_call' ||
        (p.type === 'system' && p.content),
    )

  // 本地有完整流式内容 → 优先保留本地 parts，避免被末轮残缺 serverParts 覆盖
  const parts = hasLocalContent ? localParts! : serverParts && serverParts.length > 0 ? serverParts : []

  // content 校准：server 的 full_content 更长时采用（本地逐 chunk 拼接可能不完整）
  const currentContent = localContent || ''
  const content =
    serverFullContent && serverFullContent.length > currentContent.length
      ? serverFullContent
      : currentContent

  return { parts, content }
}

/**
 * 从事件数据中提取消息 ID
 *
 * 统一处理 message_id 的多种来源，避免各 handler 重复写
 * `eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id` 模式。
 *
 * @param eventData - WebSocket 事件数据（顶层或嵌套 data）
 * @returns 消息 ID 字符串，找不到时返回 null
 */
export function extractMessageId(eventData: any): string | null {
  if (!eventData) return null
  return (
    eventData.message_id
    || eventData.data?.message_id
    || eventData.data?.ai_message_id
    || null
  )
}

/**
 * 统一启动管道流式状态
 *
 * pipelineStore.streamingState 是唯一数据源。
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param messageId - 正在流式传输的消息 ID
 */
export function startPipelineStreaming(
  pipelineId: string,
  messageId: string,
): void {
  pipelineStore.getState().startStreaming(pipelineId, messageId)
}

/**
 * 停止管道流式传输
 *
 * @param pipelineId - 管道 ID（唯一路由键）
 * @param threadId - 可选的会话 ID，threadId 与 pipelineId 不同时一并清理
 */
export function stopPipelineStreaming(pipelineId: string, threadId?: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
  if (threadId && threadId !== pipelineId) {
    pipelineStore.getState().stopStreaming(threadId)
  }
}

/**
 * 分配下一个 sequence 值。
 * - 后端消息：直接使用后端 sequence，但不小于本地已有最大值（防止后端计数器未续接）
 * - 用户消息：使用本地最大值 + 1（乐观更新，等后端覆盖）
 *
 * @param pipelineId - 管道 ID
 * @param backendSequence - 后端返回的真实 sequence（WS 事件携带）
 */
export function allocateNextSequence(pipelineId: string, backendSequence?: number): number {
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const localMax = existingMsgs.reduce(
    (max: number, m: any) => Math.max(max, m.sequence ?? 0), 0,
  )
  if (backendSequence != null && backendSequence > 0) {
    return Math.max(backendSequence, localMax + 1)
  }
  return localMax + 1
}

/**
 * 分配 Part 级 sequence 的 fallback 值（后端事件未携带 sequence 时）。
 *
 * 渲染层 buildFragmentsFromParts 按 part.sequence 数值升序渲染。若 fallback 用
 * Date.now()（毫秒大数），缺失 sequence 的 part 会必然排到所有后端小整数
 * sequence 的 part 之后——例如思考 part 被排到正文文本下方，呈现"思考顺序错乱"。
 *
 * 正确语义：fallback 必须保持与已有 parts 的相对顺序，取当前消息 parts 的
 * 最大 sequence + 1，使新创建的 part 紧跟在已渲染内容之后（与流式到达顺序一致）。
 *
 * BUG-FIX-fix_20260627_thinking_render_order:
 *   问题根因: thinkingHandler / streamHandler 在后端 sequence 缺失时用 Date.now()
 *   作 fallback，导致思考 part 排到文本 part 之后（刷新后依旧，因同一数据源）。
 *   修复方案: 用 part 级 max+1 替代 Date.now()，保持相对顺序。
 *
 * @param pipelineId - 管道 ID
 * @param messageId - 消息 ID
 * @returns 当前消息 parts 最大 sequence + 1（无 parts 时返回 0）
 */
export function allocatePartSequence(pipelineId: string, messageId: string): number {
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  const parts = msg?.parts || []
  const maxSeq = parts.reduce(
    (max: number, p: any) => Math.max(max, typeof p.sequence === 'number' ? p.sequence : 0), 0,
  )
  return maxSeq + 1
}

/**
 * 确保流式占位符消息存在
 *
 * 合并 startStreaming + setStreamingForTab + addMessage 三步操作，
 * 当 stream_start 丢失或 chunk 先于 start 到达时自动创建占位符。
 *
 * 同时清理同管道中旧的 streaming 占位消息（引擎唤醒/reset_for_new_turn 后
 * message_id 变化，旧占位消息残留会导致空气泡）。
 */
export function ensureStreamingPlaceholder(
  pipelineId: string,
  messageId: string,
  threadId?: string,
  backendSequence?: number,
): void {
  startPipelineStreaming(pipelineId, messageId, threadId)

  const store = pipelineStore.getState()
  const existing = store.getMessages(pipelineId)
  for (const msg of existing) {
    if (
      msg.role === 'assistant'
      && msg.status === 'streaming'
      && msg.id !== messageId
    ) {
      // BUG-FIX-fix_20260603_stale_streaming_cleanup:
      // 问题根因: 旧 streaming 占位符的清理逻辑只检查了 content/parts 是否有内容，
      //   但如果 parts 中只有 tool_call 且处于 calling 状态（未收到 tool_result），
      //   这些残留消息被标记 completed 后会与新的流式消息合并，造成渲染混乱。
      // 修复方案: 检查 tool_call parts 的解析状态。
      //   - 有未解析的 tool_call（calling）→ remove（不完整消息，直接丢弃）
      //   - 所有 tool_call 已解析 + 有内容 → 标记 completed 保留
      //   - 完全无内容 → remove
      // 影响范围: 流式过程切换时旧占位符的清理
      // 修复日期: 2026-06-03
      const parts = msg.parts || []
      const hasTextContent = (msg.content || '').length > 0
      const hasParts = parts.length > 0
      const unresolvedToolCalls = parts.some(
        (p: any) => p.type === 'tool_call' && (p.state === 'calling' || p.state === 'streaming')
      )
      const resolvedParts = parts.filter(
        (p: any) => p.type !== 'tool_call' || (p.state !== 'calling' && p.state !== 'streaming')
      )

      if (unresolvedToolCalls) {
        // 有未解析的 tool_call → 消息不完整，直接移除
        store.removeMessage(pipelineId, msg.id)
      } else if (hasTextContent || resolvedParts.length > 0) {
        // 有完整内容 → 保留但标记 completed，同时确保 tool parts 为 done
        const finalizedParts = resolvedParts.map((p: any) =>
          p.type === 'tool_call' ? { ...p, state: 'done' as const } : p
        )
        store.updateMessage(pipelineId, msg.id, {
          status: 'completed',
          parts: finalizedParts.length > 0 ? finalizedParts : undefined,
        } as any)
      } else {
        // 完全空消息 → 移除
        store.removeMessage(pipelineId, msg.id)
      }
    }
  }

  // BUG-FIX-fix_20260615_user_msg_order:
  // assistant 占位消息也要走本地 sequence 计数器（Math.max(后端 seq, 本地 max+1)），
  // 否则后端 seq（小数字）会小于已分配的 user 消息 seq → assistant 排到 user 之前。
  const placeholderSeq = allocateNextSequence(pipelineId, backendSequence)

  store.addMessage(pipelineId, {
    id: messageId,
    sessionId: threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: placeholderSeq,
    status: 'streaming',
  } as any)
}

/**
 * 从事件数据中提取 threadId
 *
 * 统一处理 `eventData.data?._threadId || eventData._threadId` 模式。
 */
export function extractThreadId(eventData: any): string | undefined {
  return eventData.data?._threadId || eventData._threadId
}

/**
 * 终止管道：清理 streamingState
 *
 * 仅在 stream_end / stream_error 等终止事件到达时调用。
 * 不再做超时兜底（chunkTimeout 已删除），后端必须主动发终止事件。
 */
export function terminatePipeline(pipelineId: string, threadId?: string): void {
  stopPipelineStreaming(pipelineId, threadId)
}

/**
 * 解析 pipelineId 并执行空值守卫 + warn 日志
 *
 * 返回 null 表示 pipelineId 为空，调用方应跳过处理。
 */
export function resolveRequiredPipelineId(eventData: any, context: string): string | null {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // BUG-FIX-M03: WS handler 层 console 残留
    // 问题根因: pipelineId 空值守卫用 console.warn 记录。
    // 修复方案: 改用正式 logger.warn。
    loggers.websocket.warn('[streaming] %s: pipelineId 为空，跳过事件', context)
    return null
  }
  return pipelineId
}
