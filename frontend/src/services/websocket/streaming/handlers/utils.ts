/** 流式事件处理器公共工具函数 统一抽取的消息 ID 提取、流式占位符创建、Streaming 状态管理， */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

/** 合并本地流式累积的 parts 与后端 stream_end/new_message 下发的 serverParts。 */
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

  // 本地有完整流式内容 → 优先保留本地 parts，避免被末轮残缺 serverParts 覆盖。
  // 同时收敛残留 state='streaming' 的 text/thinking part 为 'done'：stream_end 已标志
  // 流终止，残留的 streaming 状态通常来自 thinking_end 丢失/乱序，不收尾会让卡片图标
  // 一直转圈（参见 streamTimingRepro 场景3）。仅在实际存在残留时才新建数组，保持引用稳定。
  let parts: any[]
  if (hasLocalContent) {
    const needsFinalize = localParts!.some(
      (p) => (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming',
    )
    parts = needsFinalize
      ? localParts!.map((p) =>
          (p.type === 'text' || p.type === 'thinking') && p.state === 'streaming'
            ? { ...p, state: 'done' as const }
            : p,
        )
      : localParts!
  } else {
    parts = serverParts && serverParts.length > 0 ? serverParts : []
  }

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
}

/** 停止管道流式传输 */
export function stopPipelineStreaming(pipelineId: string, threadId?: string): void {
  pipelineStore.getState().stopStreaming(pipelineId)
  if (threadId && threadId !== pipelineId) {
    pipelineStore.getState().stopStreaming(threadId)
  }
}

/** 分配下一个 sequence 值。 - 后端消息：直接使用后端 sequence（后端 emit_notification / new_message / stream_end 都从 _entry.next_sequence() 共享计数器取值，是权威值，与流式输出共序，保证相对顺序正确）。 - 本地乐观消息（无 backendSequence，如 stream_start 占位、用户刚发的消息）：用 localMax+1 占位，待后端权威值到达后由 initFromAPI 对账纠正。 不再用 Math.max(backendSeq, localMax+1) 抬升后端权威值——那会让延迟到达的系统通知（后端 seq 早于本地 localMax）被错误排到后续 AI 回复之后。 */
export function allocateNextSequence(pipelineId: string, backendSequence?: number): number {
  if (backendSequence != null && backendSequence > 0) {
    return backendSequence
  }
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const localMax = existingMsgs.reduce(
    (max: number, m: any) => Math.max(max, m.sequence ?? 0), 0,
  )
  return localMax + 1
}

/** 确保流式占位符消息存在 合并 startStreaming + setStreamingForTab + addMessage 三步操作， */
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
      && !msg.id.startsWith('placeholder_')
    ) {
      // 这些残留消息被标记 completed 后会与新的流式消息合并，造成渲染混乱。
      // - 所有 tool_call 已解析 + 有内容 → 标记 completed 保留
      // - 完全无内容 → remove
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

  // 新 AI 消息来了，看前一条是什么：
  // - 前一条是 user/system（或没有前一条）→ 新建独立气泡
  // - 前一条是 assistant 且仍处于 streaming → 合并到前一条（不新建气泡，新内容追加进去）
  // 前一条 assistant 已 completed 时 id 冻结，绝不改写：否则会污染已完成消息的权威 id，
  // 导致后续 chunk 按 hex id 查找命中错误消息（断线重连场景的空气泡根因）。
  const after = store.getMessages(pipelineId)
  const prevMsg = after[after.length - 1]
  // ★ 诊断：stream_start 到达时的 store 状态
  loggers.websocket.info(
    '[STREAM-ARRIVE] total=%d prev=[%s/%s/%s] newMsg=%s willMerge=%s',
    after.length,
    prevMsg?.role ?? 'null',
    prevMsg?.status ?? 'null',
    (prevMsg?.id ?? '').slice(0, 10),
    messageId.slice(0, 10),
    prevMsg?.role === 'assistant' && prevMsg?.status === 'streaming',
  )
  if (prevMsg && prevMsg.role === 'assistant' && prevMsg.status === 'streaming') {
    // 合并到前一条 AI：把它的 id 更新为本轮的新 messageId，
    // 这样后续 stream_chunk / tool_start 按新 messageId 操作时落到同一条消息。
    // 同时重新置为 streaming，继续接收新内容。
    store.updateMessage(pipelineId, prevMsg.id, {
      id: messageId,
      status: 'streaming',
    } as any)
    return
  }

  // 前一条是 user/system 或没有前一条 → 新建独立气泡
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

/** 从事件数据中提取 threadId 统一处理 `eventData.data?._threadId || eventData._threadId` 模式。 */
export function extractThreadId(eventData: any): string | undefined {
  return eventData.data?._threadId || eventData._threadId
}

/** 终止管道：清理 streamingState 仅在 stream_end / stream_error 等终止事件到达时调用。 */
export function terminatePipeline(pipelineId: string, threadId?: string): void {
  stopPipelineStreaming(pipelineId, threadId)
}

/** 解析 pipelineId 并执行空值守卫 + warn 日志 返回 null 表示 pipelineId 为空，调用方应跳过处理。 */
export function resolveRequiredPipelineId(eventData: any, context: string): string | null {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // -M03: WS handler 层 console 残留
    loggers.websocket.warn('[streaming] %s: pipelineId 为空，跳过事件', context)
    return null
  }
  return pipelineId
}
