/**
 * 全局流式事件服务（管道 ID 路由版本）
 *
 * 核心设计原则：
 * - pipeline_id 是唯一的路由键
 * - 所有消息操作统一通过 pipelineMessageStore
 * - 每个管道独立渲染，互不干扰
 * - 主管道是默认管道（无 pipeline_id 时用 session_id 充当）
 * - 后台会话的管道也能独立流式输出
 */

import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

const _debugLogger = loggers.websocket

const CHUNK_INTERVAL_TIMEOUT_MS = 60_000

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}

interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
  /** 计时器启动时的单调时间戳（毫秒），用于页面后台期间暂停计时 */
  startedAt: number
  /** 页面可见时剩余的超时毫秒数 */
  remainingMs: number
  /** 是否处于暂停状态（页面不可见时暂停） */
  paused: boolean
  /** 暂停时记录的时间戳，用于计算已消耗时间 */
  pausedAt: number
}
const _chunkTimeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

/**
 * 解析事件的 pipeline_id
 *
 * 优先级：data.pipeline_id（非空字符串）> null
 *
 * BUG-FIX-fix_20260511_message_cross_talk:
 * 问题根因: 后端 TargetedSink 定向路由失败时回退到 broadcast_event()，
 *          事件被发送到所有 WebSocket 连接。每个连接池连接会为事件打上自己的 _threadId。
 *          原逻辑 data.pipeline_id || _threadId || null 会在 pipeline_id 为空字符串时
 *          走到 _threadId fallback，导致同一事件被路由到不同的管道，造成消息串扰。
 * 修复方案: 严格校验 pipeline_id（空字符串视为无效），不再使用 _threadId 作为 fallback。
 *          _threadId 仅用于 streamingStore 的双 key 配对（与 handleStreamStart 配合），
 *          不参与消息路由。
 */
function resolvePipelineId(eventData: any): string | null {
  const pid = eventData.data?.pipeline_id
  return (typeof pid === 'string' && pid.length > 0) ? pid : null
}

/**
 * 重置流式块超时计时器
 *
 * 页面可见性感知：只在页面可见时才真正启动超时计时器，
 * 页面隐藏时暂停计时，恢复可见后继续倒计时。
 */
function resetChunkTimeout(pipelineId: string, messageId: string): void {
  clearChunkTimeout(pipelineId)
  const now = performance.now()
  const isPageVisible = !document.hidden
  const entry: ChunkTimeoutEntry = {
    timer: null!,
    messageId,
    startedAt: now,
    remainingMs: CHUNK_INTERVAL_TIMEOUT_MS,
    paused: !isPageVisible,
    pausedAt: isPageVisible ? 0 : now,
  }
  if (isPageVisible) {
    entry.timer = setTimeout(() => _onChunkTimeout(pipelineId), CHUNK_INTERVAL_TIMEOUT_MS)
  }
  _chunkTimeoutMap.set(pipelineId, entry)
}

/**
 * chunk 超时回调：标记流式响应中断
 */
function _onChunkTimeout(pipelineId: string): void {
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (!entry) return
  _chunkTimeoutMap.delete(pipelineId)
  useStreamingStore.getState().stopStreamingForTab(pipelineId)
  pipelineStore.getState().updateMessage(pipelineId, entry.messageId, {
    content: '\n\n⚠️ 流式响应中断，请重试。',
    status: 'error',
  } as any)

  useNotificationStore.getState().addNotification({
    title: '响应中断',
    message: '流式响应超时中断，请重新发送消息',
    priority: 'high',
    category: 'error',
    isBlocking: false,
  })
}

/**
 * 页面变为不可见时暂停所有 chunk 超时计时器
 */
function pauseAllChunkTimeouts(): void {
  const now = performance.now()
  for (const [pipelineId, entry] of _chunkTimeoutMap) {
    if (entry.paused) continue
    clearTimeout(entry.timer)
    const elapsed = now - entry.startedAt
    entry.remainingMs = Math.max(0, entry.remainingMs - elapsed)
    entry.paused = true
    entry.pausedAt = now
    _debugLogger.debug(`[CHUNK_TIMEOUT] 暂停: pipeline=%s remaining=%dms`, pipelineId.slice(0, 8), entry.remainingMs)
  }
}

/**
 * 页面恢复可见时恢复所有 chunk 超时计时器
 */
function resumeAllChunkTimeouts(): void {
  const now = performance.now()
  for (const [pipelineId, entry] of _chunkTimeoutMap) {
    if (!entry.paused) continue
    entry.paused = false
    entry.startedAt = now
    if (entry.remainingMs <= 0) {
      _onChunkTimeout(pipelineId)
    } else {
      entry.timer = setTimeout(() => _onChunkTimeout(pipelineId), entry.remainingMs)
      _debugLogger.debug(`[CHUNK_TIMEOUT] 恢复: pipeline=%s remaining=%dms`, pipelineId.slice(0, 8), entry.remainingMs)
    }
  }
}

/**
 * 清除指定管道的超时计时器
 */
function clearChunkTimeout(pipelineId: string): void {
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (entry) {
    clearTimeout(entry.timer)
    _chunkTimeoutMap.delete(pipelineId)
  }
}

/**
 * 清除所有超时计时器
 */
function clearAllChunkTimeouts(): void {
  for (const [, entry] of _chunkTimeoutMap) clearTimeout(entry.timer)
  _chunkTimeoutMap.clear()
}

/**
 * 追加文本内容到 contentBlocks
 */
function appendTextBlock(prevBlocks: any[], content: string, messageId: string): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastBlock = blocks[blocks.length - 1]
  if (lastBlock?.type === 'text') {
    blocks[blocks.length - 1] = { ...lastBlock, text: (lastBlock.text || '') + content }
  } else {
    blocks.push({ type: 'text', text: content, sourceId: messageId })
  }
  return blocks
}

/**
 * 追加思考内容到 contentBlocks
 */
function appendThinkingChunk(prevBlocks: any[], chunk: string): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastIdx = blocks.findLastIndex((b) => b.type === 'thinking' && b.thinking?.isThinking)
  if (lastIdx !== -1) {
    const block = { ...blocks[lastIdx] }
    block.thinking = { content: (block.thinking?.content || '') + chunk, isThinking: true }
    blocks[lastIdx] = block
  }
  return blocks
}

/**
 * 结束思考块，将 isThinking 设为 false
 */
function endThinkingBlock(prevBlocks: any[]): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastIdx = blocks.findLastIndex((b) => b.type === 'thinking' && b.thinking?.isThinking)
  if (lastIdx !== -1) {
    const block = { ...blocks[lastIdx] }
    block.thinking = { content: block.thinking?.content || '', isThinking: false }
    blocks[lastIdx] = block
  }
  return blocks
}

// ─── 事件处理器 ───

/**
 * 处理流式开始事件
 */
function handleStreamStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const messageId = eventData.message_id || eventData.data?.message_id
  _debugLogger.info(
    `[STREAM_START] pipelineId=%s messageId=%s _threadId=%s dataKeys=%s`,
    pipelineId, messageId, eventData._threadId,
    eventData.data ? Object.keys(eventData.data).join(',') : '(no data)',
  )
  if (!pipelineId) return
  if (!messageId) return

  pipelineStore.getState().startStreaming(pipelineId, messageId)

  // BUG-FIX-fix_20260509_tab_streaming:
  // 问题根因: handleStreamStart 未调用 setStreamingForTab，导致 streamingTabs 始终为空，
  //          ChatContainer 的 effectiveIsGenerating 始终为 false，子 Tab 无法显示流式状态。
  // 修复方案: 在流式开始时通过 pipelineId 设置 streaming 状态。
  // 影响范围: 所有标签页的流式指示器和停止按钮。
  useStreamingStore.getState().setStreamingForTab(pipelineId, true)

  // BUG-FIX-fix_20260511_streaming_key_mismatch:
  // 问题根因: router.tsx 和 ChatContainer 使用 sessionId (thread_id) 查找 streamingTabs，
  //          但 setStreamingForTab 只用 pipelineId 作为 key。主管道场景下 pipelineId ≠ thread_id，
  //          导致 effectiveIsGenerating 始终为 true（stream_end 未清除 thread_id 对应的 key），
  //          前端输入框被禁用，用户无法发送第二条消息。
  // 修复方案: 同时用 _threadId 设置 streaming 状态，确保两套 key 都能命中。
  // 影响范围: 主管道多轮对话的输入框可用性。
  const threadId = eventData._threadId
  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, true)
  }

  resetChunkTimeout(pipelineId, messageId)

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const nextSeq = existingMsgs.reduce((max: number, m: any) => Math.max(max, m.sequence ?? 0), 0) + 1

  pipelineStore.getState().addMessage(pipelineId, {
    id: messageId,
    sessionId: eventData._threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: nextSeq,
    status: 'streaming',
    contentBlocks: [],
  } as any)
}

/**
 * 处理流式块事件
 */
function handleStreamChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const content = eventData.content || eventData.data?.content || ''
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  let msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) {
    _debugLogger.warn(
      `[STREAM_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s totalMsgs=%d _threadId=%s`,
      pipelineId, messageId, msgs.length, eventData._threadId,
    )

    const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
    const nextSeq = existingMsgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1

    pipelineStore.getState().addMessage(pipelineId, {
      id: messageId,
      sessionId: eventData._threadId || '',
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: nextSeq,
      status: 'streaming',
      contentBlocks: [],
    } as any)

    useStreamingStore.getState().setStreamingForTab(pipelineId, true)

    msg = pipelineStore.getState().getMessages(pipelineId).find((m: any) => m.id === messageId)
    if (!msg) return
  }

  if ((msg as any).thinking?.isThinking) {
    const blocks = appendThinkingChunk(msg.contentBlocks, content)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      thinking: { content: ((msg as any).thinking.content || '') + content, isThinking: true },
      contentBlocks: blocks,
    } as any)
  } else {
    const blocks = appendTextBlock(msg.contentBlocks, content, messageId)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      contentBlocks: blocks,
      content: (msg.content || '') + content,
    } as any)
  }
}

/**
 * 处理流式结束事件
 *
 * 关键修改：不再无条件调用 reconcileContentBlocks，
 * 而是先检查 hasTextBlocks 再决定是否需要对齐。
 */
function handleStreamEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const fallbackPipelineId = pipelineId || eventData._threadId || null

  if (fallbackPipelineId) {
    clearChunkTimeout(fallbackPipelineId)
    pipelineStore.getState().stopStreaming(fallbackPipelineId)
    useStreamingStore.getState().setStreamingForTab(fallbackPipelineId, false)
  }

  // BUG-FIX-fix_20260511_streaming_key_mismatch:
  // 同时清除 _threadId 对应的 streaming 状态，与 handleStreamStart 中的设置配对。
  const threadId = eventData._threadId
  if (threadId && threadId !== fallbackPipelineId) {
    clearChunkTimeout(threadId)
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }

  if (!pipelineId) return

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  const messageId = eventData?.message_id || eventData?.data?.message_id
  const fullContent = eventData?.full_content || eventData?.data?.full_content
  if (!messageId) return

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const finalContent = fullContent || msg.content || ''
  const finalThinking = (msg as any).thinking
    ? { ...(msg as any).thinking, isThinking: false }
    : undefined

  const existingBlocks = msg.contentBlocks || []
  const hasTextBlocks = existingBlocks.some((b: any) => b.type === 'text' && b.text?.trim())

  let finalBlocks: any[]
  if (hasTextBlocks) {
    finalBlocks = existingBlocks.map((block: any) => {
      if (block.type === 'thinking' && block.thinking) {
        return { ...block, thinking: { ...block.thinking, isThinking: false } }
      }
      return block
    })
  } else if (finalContent.trim()) {
    finalBlocks = reconcileContentBlocks(
      existingBlocks, finalContent, (msg as any).toolCalls, finalThinking, messageId,
    )
  } else {
    finalBlocks = existingBlocks
  }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
    content: finalContent,
    contentBlocks: finalBlocks,
    _reconciled: true,
    ...(finalThinking ? { thinking: finalThinking } : {}),
  } as any)
}

/**
 * 处理新消息事件
 *
 * 采用与 handleStreamEnd 相同的 hasTextBlocks 逻辑。
 */
function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const fallbackPipelineId = pipelineId || eventData._threadId || null

  if (fallbackPipelineId) {
    clearChunkTimeout(fallbackPipelineId)
    useStreamingStore.getState().stopStreamingForTab(fallbackPipelineId)
    pipelineStore.getState().stopStreaming(fallbackPipelineId)
  }

  const threadId = eventData._threadId
  if (threadId && threadId !== fallbackPipelineId) {
    clearChunkTimeout(threadId)
    useStreamingStore.getState().stopStreamingForTab(threadId)
  }

  if (!pipelineId) return

  const messageId = eventData?.message_id || eventData?.message?.id || eventData?.data?.message_id || eventData?.data?.id
  if (!messageId) return

  const finalContent = eventData?.content || eventData?.data?.content
  const data = eventData?.data || eventData

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const existing = msgs.find((m: any) => m.id === messageId)
  if (!existing) return

  if ((existing as any)._reconciled) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
    } as any)
  } else if (finalContent) {
    const ft = existing.thinking ? { ...existing.thinking, isThinking: false } : undefined
    const existingBlocks = existing.contentBlocks || []
    const hasTextBlocks = existingBlocks.some((b: any) => b.type === 'text' && b.text?.trim())

    let rb: any[]
    if (hasTextBlocks) {
      rb = existingBlocks.map((block: any) => {
        if (block.type === 'thinking' && block.thinking) {
          return { ...block, thinking: { ...block.thinking, isThinking: false } }
        }
        return block
      })
    } else {
      rb = reconcileContentBlocks(existingBlocks, finalContent, existing.toolCalls, ft, messageId)
    }

    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      content: finalContent,
      contentBlocks: rb,
      _reconciled: true,
      ...(ft ? { thinking: ft } : {}),
    } as any)
  } else {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
    } as any)
  }
}

/**
 * 处理思考开始事件
 */
function handleThinkingStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  if ((msg as any).thinking?.isThinking) return
  if ((msg.contentBlocks || []).some((b: any) => b.type === 'thinking' && b.thinking?.isThinking)) return

  const thinkingBlock = { type: 'thinking' as const, thinking: { content: '', isThinking: true } as any, sourceId: messageId }
  const blocks = [...(msg.contentBlocks || []), thinkingBlock]
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: '', isThinking: true },
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理思考块事件
 */
function handleThinkingChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const chunk = eventData.content || eventData.data?.content || ''
  if (!messageId || !chunk) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!(msg as any)?.thinking) return

  const blocks = appendThinkingChunk(msg.contentBlocks, chunk)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: ((msg as any).thinking.content || '') + chunk, isThinking: true },
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理思考结束事件
 */
function handleThinkingEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!(msg as any)?.thinking) return

  const blocks = endThinkingBlock(msg.contentBlocks)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: (msg as any).thinking.content || '', isThinking: false },
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理工具调用开始事件
 */
function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  console.warn(
    `%c[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    'color:orange;font-weight:bold',
    toolName, callId || '(no-call-id)', pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const existingCalls: any[] = msg.toolCalls || []
  const existingBlocks: any[] = msg.contentBlocks || []

  if (callId) {
    if (existingCalls.some((tc: any) => tc.call_id === callId && tc.status === 'running')) return
    if (existingBlocks.some((b: any) => b.type === 'tool_call' && b.toolCall?.call_id === callId)) return
  } else {
    const runningCount = existingCalls.filter(
      (tc: any) => tc.tool_name === toolName && tc.status === 'running',
    ).length
    if (runningCount > 0) return
    const blockRunningCount = existingBlocks.filter(
      (b: any) => b.type === 'tool_call' && b.toolCall?.tool_name === toolName && b.toolCall?.status === 'running',
    ).length
    if (blockRunningCount > 0) return
  }

  const finalCallId = callId || `call_${toolName}_${Date.now()}`
  const newToolCall = {
    call_id: finalCallId, tool_name: toolName,
    tool_args: eventData.args || eventData.data?.args || {},
    status: 'running' as const, started_at: new Date().toISOString(),
  }
  const toolBlock = { type: 'tool_call' as const, toolCall: newToolCall, sourceId: messageId }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    toolCalls: [...existingCalls, newToolCall],
    contentBlocks: [...existingBlocks, toolBlock],
  } as any)
}

/**
 * 处理工具调用结果事件
 */
function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const callId = eventData.call_id || eventData.data?.call_id

  const buildUpdated = (existing: any[]) => {
    const updated = existing.map((tc) => {
      if (tc.tool_name === toolName && tc.status === 'running') {
        return {
          ...tc,
          status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
          result: eventData.result ?? eventData.data?.result,
          completed_at: new Date().toISOString(),
          duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
        }
      }
      return tc
    })
    if (!updated.some((tc) => tc.tool_name === toolName && tc.status !== 'running')) {
      updated.push({
        call_id: callId || `call_${Date.now()}`, tool_name: toolName, tool_args: {},
        status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
        result: eventData.result ?? eventData.data?.result, completed_at: new Date().toISOString(),
        duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
      })
    }
    return updated
  }

  const patchBlocks = (prevBlocks: any[], updated: any[]) => {
    const blocks = prevBlocks ? [...prevBlocks] : []
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i]
      if (b.type === 'tool_call' && b.toolCall?.status === 'running') {
        const match = updated.find((tc) => tc.call_id === b.toolCall!.call_id && tc.status !== 'running')
        if (match) blocks[i] = { ...b, toolCall: match }
      }
    }
    return blocks
  }

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const updated = buildUpdated(msg.toolCalls || [])
  const blocks = patchBlocks(msg.contentBlocks, updated)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    toolCalls: updated,
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理子 Agent 创建事件
 */
function handleSubAgentCreated(eventData: any) {
  const data = eventData.data || eventData
  const taskId = data.taskId || data.agentId
  const pipelineId = data.pipelineId
  const parentId = data.parentId
  _debugLogger.info(
    `[SUB_AGENT_CREATED] taskId=%s pipelineId=%s parentId=%s`,
    taskId, pipelineId, parentId,
  )
  if (!taskId || !pipelineId) return

  const tabId = `sub-${parentId || taskId}`
  useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)
}

/**
 * 处理流式错误事件
 *
 * BUG-FIX-fix_20260510_streaming_stuck:
 * 当 LLM 调用失败或流式传输异常时，后端发送 stream_error 事件。
 * 前端必须清理该管道的 streaming 状态，否则输入框会一直卡在执行中。
 */
function handleStreamError(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const fallbackPipelineId = pipelineId || eventData._threadId || null

  _debugLogger.warn(
    `[STREAM_ERROR] pipelineId=%s _threadId=%s`,
    pipelineId, eventData._threadId,
  )

  if (fallbackPipelineId) {
    clearChunkTimeout(fallbackPipelineId)
    pipelineStore.getState().stopStreaming(fallbackPipelineId)
    useStreamingStore.getState().stopStreamingForTab(fallbackPipelineId)
  }

  if (!pipelineId) return

  const messageId = eventData?.message_id || eventData?.data?.message_id
  if (messageId) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'error',
    } as any)
  }

  const errorMsg = eventData?.data?.error || eventData?.error || '流式响应异常'
  useNotificationStore.getState().addNotification({
    title: '流式响应错误',
    message: typeof errorMsg === 'string' ? errorMsg : '生成过程中发生错误，请重试',
    priority: 'high',
    category: 'error',
    isBlocking: false,
  })
}

/**
 * 处理流式保活事件（压缩等长时间操作期间由后端发送）
 */
function handleStreamKeepalive(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const entry = _chunkTimeoutMap.get(pipelineId)
  if (entry) {
    resetChunkTimeout(pipelineId, entry.messageId)
  }
}

/**
 * 页面可见性变化处理函数
 */
function _handleVisibilityChange(): void {
  if (document.hidden) {
    pauseAllChunkTimeouts()
  } else {
    resumeAllChunkTimeouts()
  }
}

/**
 * 初始化全局流式事件处理器
 */
export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  _handlers[WS_SERVER_EVENTS.STREAM_START] = handleStreamStart
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = handleStreamChunk
  _handlers[WS_SERVER_EVENTS.STREAM_END] = handleStreamEnd
  _handlers[WS_SERVER_EVENTS.STREAM_ERROR] = handleStreamError
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = handleNewMessage
  _handlers[WS_SERVER_EVENTS.THINKING_START] = handleThinkingStart
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = handleThinkingChunk
  _handlers[WS_SERVER_EVENTS.THINKING_END] = handleThinkingEnd
  _handlers[WS_SERVER_EVENTS.TOOL_START] = handleToolStart
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = handleToolResult
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = handleSubAgentCreated
  _handlers[WS_SERVER_EVENTS.STREAM_KEEPALIVE] = handleStreamKeepalive

  for (const [event, handler] of Object.entries(_handlers)) {
    wsPool.subscribe(event, handler)
    globalWS.subscribe(event, handler)
  }

  document.addEventListener('visibilitychange', _handleVisibilityChange)
}

/**
 * 销毁全局流式事件处理器
 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return
  clearAllChunkTimeouts()
  document.removeEventListener('visibilitychange', _handleVisibilityChange)
  for (const [event, handler] of Object.entries(_handlers)) {
    wsPool.unsubscribe(event, handler)
    globalWS.unsubscribe(event, handler)
  }
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  _initialized = false
}
