/**
 * 全局流式事件服务（管道 ID 路由版本）
 *
 * 核心设计原则：
 * - pipeline_id 是唯一的路由键
 * - 知道 pipeline_id 就知道属于哪个会话（通过 pipelineSessionMap）
 * - 每个管道独立渲染，互不干扰
 * - 主管道是默认管道（无 pipeline_id 时用 session_id 充当）
 * - 后台会话的管道也能独立流式输出
 *
 * 路由逻辑：
 * 1. 事件有 pipeline_id → 通过 pipelineSessionMap 找到归属会话 → 路由到对应 Tab
 * 2. 事件无 pipeline_id（主管道）→ _threadId 即为归属会话 → 路由到主 Tab
 * 3. broadcast_event 跨连接串扰 → pipelineSessionMap 修正归属 → 不会写错位置
 */

import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'

const CHUNK_INTERVAL_TIMEOUT_MS = 60_000

let _initialized = false
const _handlers: Record<string, (data: any) => void> = {}

interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
}
const _chunkTimeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

/**
 * pipeline_id → session_id 映射
 *
 * 每个子管道在 sub_agent_created 时注册其归属会话。
 * 主管道不需要注册——它没有 pipeline_id，用 _threadId 代替。
 */
const pipelineSessionMap: Map<string, string> = new Map()

function registerPipelineOwner(pipelineId: string, sessionId: string): void {
  pipelineSessionMap.set(pipelineId, sessionId)
}

function getSessionByPipeline(pipelineId: string): string | undefined {
  return pipelineSessionMap.get(pipelineId)
}

/**
 * 解析事件的归属会话 ID
 *
 * 优先级：pipeline_id（查映射）> _threadId（主管道直连）
 */
function resolveSessionId(eventData: any): string | null {
  const pipelineId = eventData.pipeline_id || eventData.data?.pipeline_id
  if (pipelineId) {
    return getSessionByPipeline(pipelineId) ?? eventData._threadId ?? null
  }
  return eventData._threadId || null
}

function isActiveSession(sessionId: string | null): boolean {
  if (!sessionId) return false
  return useSessionStore.getState().activeSessionId === sessionId
}

function resolvePipelineTab(eventData: any): string | null {
  const pipelineId = eventData.pipeline_id || eventData.data?.pipeline_id
  if (!pipelineId) return null
  return useAgentTabStore.getState().getTabIdByPipeline(pipelineId) ?? null
}

function resetChunkTimeout(key: string, messageId: string, pipelineTabId: string | null): void {
  clearChunkTimeout(key)
  const timer = setTimeout(() => {
    _chunkTimeoutMap.delete(key)
    useStreamingStore.getState().stopStreamingForTab(key)
    if (pipelineTabId) {
      updateSubTabMessage(pipelineTabId, messageId, {
        content: '\n\n⚠️ 流式响应中断，请重试。',
        status: 'error',
      })
    }
  }, CHUNK_INTERVAL_TIMEOUT_MS)
  _chunkTimeoutMap.set(key, { timer, messageId })
}

function clearChunkTimeout(key: string): void {
  const entry = _chunkTimeoutMap.get(key)
  if (entry) {
    clearTimeout(entry.timer)
    _chunkTimeoutMap.delete(key)
  }
}

function clearAllChunkTimeouts(): void {
  for (const [, entry] of _chunkTimeoutMap) clearTimeout(entry.timer)
  _chunkTimeoutMap.clear()
}

function getSubTabMessage(tabId: string, messageId: string): any | undefined {
  const tabMsgs = useAgentTabStore.getState().tabMessages[tabId] || []
  return tabMsgs.find((m) => m.id === messageId)
}

function updateSubTabMessage(tabId: string, messageId: string, updates: Record<string, any>): void {
  const store = useAgentTabStore.getState()
  const tabMsgs = store.tabMessages[tabId] || []
  const msg = tabMsgs.find((m) => m.id === messageId)
  if (msg) store.addMessageToTab(tabId, { ...msg, ...updates })
}

function addMessageToSession(sessionId: string, messageId: string, extra?: Partial<any>): void {
  const msgs = useSessionStore.getState().messages[sessionId] || []
  const nextSeq = msgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
  useSessionStore.getState().addMessage(sessionId, {
    id: messageId,
    sessionId,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: nextSeq,
    status: 'streaming',
    contentBlocks: [],
    ...extra,
  })
}

function updateMessageInSession(sessionId: string, messageId: string, updates: Record<string, any>): void {
  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return
  const merged = { ...msg, ...updates }
  const { setMessages } = useSessionStore.getState()
  const updated = msgs.map((m) => m.id === messageId ? merged : m)
  setMessages(sessionId, updated)
}

/**
 * 计算流式 key
 *
 * 活跃会话：子 Tab 用 tabId，主 Tab 用 session 对应的 main key
 * 后台会话：用 bg:sessionId
 */
function computeStreamingKey(sessionId: string, pipelineTabId: string | null, active: boolean): string {
  if (active && pipelineTabId) return pipelineTabId
  if (active) return sessionId
  return `bg:${sessionId}`
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

function handleStreamStart(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null
  const key = computeStreamingKey(sessionId, pipelineTabId, active)

  const { setStreamingForTab } = useStreamingStore.getState()
  setStreamingForTab(key, true)
  resetChunkTimeout(key, messageId, pipelineTabId)

  if (active && pipelineTabId) {
    useAgentTabStore.getState().addMessageToTab(pipelineTabId, {
      id: messageId, sessionId, role: 'assistant', content: '',
      timestamp: new Date().toISOString(), parentId: null, sequence: 0,
      status: 'streaming', contentBlocks: [],
    })
  } else if (active) {
    addMessageToSession(sessionId, messageId)
  } else {
    addMessageToSession(sessionId, messageId)
  }
}

function handleStreamChunk(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const content = eventData.content || eventData.data?.content || ''
  if (!messageId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null
  const key = computeStreamingKey(sessionId, pipelineTabId, active)

  resetChunkTimeout(key, messageId, pipelineTabId)

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    if (msg?.thinking?.isThinking) {
      const blocks = appendThinkingChunk(msg.contentBlocks, content)
      updateSubTabMessage(pipelineTabId, messageId, {
        thinking: { content: (msg.thinking.content || '') + content, isThinking: true },
        contentBlocks: blocks,
      })
    } else {
      const blocks = appendTextBlock(msg?.contentBlocks, content, messageId)
      updateSubTabMessage(pipelineTabId, messageId, {
        contentBlocks: blocks, content: (msg?.content || '') + content,
      })
    }
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return

  if (msg.thinking?.isThinking) {
    const blocks = appendThinkingChunk(msg.contentBlocks, content)
    updateMessageInSession(sessionId, messageId, {
      thinking: { content: (msg.thinking.content || '') + content, isThinking: true },
      contentBlocks: blocks,
    })
  } else {
    const blocks = appendTextBlock(msg.contentBlocks, content, messageId)
    updateMessageInSession(sessionId, messageId, {
      contentBlocks: blocks, content: (msg.content || '') + content,
    })
  }
}

function handleStreamEnd(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null
  const key = computeStreamingKey(sessionId, pipelineTabId, active)

  clearChunkTimeout(key)
  useStreamingStore.getState().stopStreamingForTab(key)

  const messageId = eventData?.message_id || eventData?.data?.message_id
  const fullContent = eventData?.full_content || eventData?.data?.full_content
  if (!messageId) return

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const finalContent = fullContent || msg?.content || ''
    const finalThinking = msg?.thinking ? { ...msg.thinking, isThinking: false } : undefined
    const reconciled = reconcileContentBlocks(msg?.contentBlocks, finalContent, msg?.toolCalls, finalThinking, messageId)
    updateSubTabMessage(pipelineTabId, messageId, {
      status: 'completed', content: finalContent, contentBlocks: reconciled,
      _reconciled: true, ...(finalThinking ? { thinking: finalThinking } : {}),
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return

  const finalContent = fullContent || msg.content || ''
  const finalThinking = msg.thinking ? { ...msg.thinking, isThinking: false } : undefined
  const reconciled = reconcileContentBlocks(msg.contentBlocks, finalContent, msg.toolCalls, finalThinking, messageId)
  updateMessageInSession(sessionId, messageId, {
    status: 'completed', content: finalContent, contentBlocks: reconciled,
    _reconciled: true, ...(finalThinking ? { thinking: finalThinking } : {}),
  })
}

function handleNewMessage(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null
  const key = computeStreamingKey(sessionId, pipelineTabId, active)

  clearChunkTimeout(key)
  useStreamingStore.getState().stopStreamingForTab(key)

  const messageId = eventData?.message_id || eventData?.message?.id || eventData?.data?.message_id || eventData?.data?.id
  if (!messageId) return

  const finalContent = eventData?.content || eventData?.data?.content

  if (active && pipelineTabId) {
    const tabMsgs = useAgentTabStore.getState().tabMessages[pipelineTabId] || []
    const existing = tabMsgs.find((m) => m.id === messageId)
    if (existing && (existing as any)._reconciled) {
      updateSubTabMessage(pipelineTabId, messageId, { status: 'completed' })
    } else if (existing && finalContent) {
      const ft = existing.thinking ? { ...existing.thinking, isThinking: false } : undefined
      const rb = reconcileContentBlocks(existing.contentBlocks, finalContent, existing.toolCalls, ft, messageId)
      updateSubTabMessage(pipelineTabId, messageId, {
        status: 'completed', content: finalContent, contentBlocks: rb,
        _reconciled: true, ...(ft ? { thinking: ft } : {}),
      })
    } else if (existing) {
      updateSubTabMessage(pipelineTabId, messageId, { status: 'completed' })
    }
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const existing = msgs.find((m) => m.id === messageId)
  if (existing && (existing as any)._reconciled) {
    updateMessageInSession(sessionId, messageId, { status: 'completed' })
  } else if (existing && finalContent) {
    const ft = existing.thinking ? { ...existing.thinking, isThinking: false } : undefined
    const rb = reconcileContentBlocks(existing.contentBlocks, finalContent, existing.toolCalls, ft, messageId)
    updateMessageInSession(sessionId, messageId, {
      status: 'completed', content: finalContent, contentBlocks: rb,
      _reconciled: true, ...(ft ? { thinking: ft } : {}),
    })
  } else if (existing) {
    updateMessageInSession(sessionId, messageId, { status: 'completed' })
  }
}

function handleThinkingStart(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null
  const thinkingBlock = { type: 'thinking' as const, thinking: { content: '', isThinking: true } as any, sourceId: messageId }

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const blocks = [...(msg?.contentBlocks || []), thinkingBlock]
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: '', isThinking: true }, contentBlocks: blocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return
  const blocks = [...(msg.contentBlocks || []), thinkingBlock]
  updateMessageInSession(sessionId, messageId, {
    thinking: { content: '', isThinking: true }, contentBlocks: blocks,
  })
}

function handleThinkingChunk(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const chunk = eventData.content || eventData.data?.content || ''
  if (!messageId || !chunk) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const prevContent = msg?.thinking?.content || ''
    const blocks = appendThinkingChunk(msg?.contentBlocks, chunk)
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: prevContent + chunk, isThinking: true }, contentBlocks: blocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg?.thinking) return
  const blocks = appendThinkingChunk(msg.contentBlocks, chunk)
  updateMessageInSession(sessionId, messageId, {
    thinking: { content: msg.thinking.content + chunk, isThinking: true }, contentBlocks: blocks,
  })
}

function handleThinkingEnd(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const blocks = endThinkingBlock(msg?.contentBlocks)
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: msg?.thinking?.content || '', isThinking: false }, contentBlocks: blocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg?.thinking) return
  const blocks = endThinkingBlock(msg.contentBlocks)
  updateMessageInSession(sessionId, messageId, {
    thinking: { content: msg.thinking.content || '', isThinking: false }, contentBlocks: blocks,
  })
}

function handleToolStart(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id || `call_${Date.now()}`
  const newToolCall = {
    call_id: callId, tool_name: toolName,
    tool_args: eventData.args || eventData.data?.args || {},
    status: 'running' as const, started_at: new Date().toISOString(),
  }
  const toolBlock = { type: 'tool_call' as const, toolCall: newToolCall, sourceId: messageId }

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const existing = msg?.toolCalls || []
    const blocks = [...(msg?.contentBlocks || []), toolBlock]
    updateSubTabMessage(pipelineTabId, messageId, { toolCalls: [...existing, newToolCall], contentBlocks: blocks })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return
  const existing = msg.toolCalls || []
  const blocks = [...(msg.contentBlocks || []), toolBlock]
  updateMessageInSession(sessionId, messageId, { toolCalls: [...existing, newToolCall], contentBlocks: blocks })
}

function handleToolResult(eventData: any) {
  const sessionId = resolveSessionId(eventData)
  if (!sessionId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

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

  const active = isActiveSession(sessionId)
  const pipelineTabId = active ? resolvePipelineTab(eventData) : null

  if (active && pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const updated = buildUpdated(msg?.toolCalls || [])
    const blocks = patchBlocks(msg?.contentBlocks, updated)
    updateSubTabMessage(pipelineTabId, messageId, { toolCalls: updated, contentBlocks: blocks })
    return
  }

  const msgs = useSessionStore.getState().messages[sessionId] || []
  const msg = msgs.find((m) => m.id === messageId)
  if (!msg) return
  const updated = buildUpdated(msg.toolCalls || [])
  const blocks = patchBlocks(msg.contentBlocks, updated)
  updateMessageInSession(sessionId, messageId, { toolCalls: updated, contentBlocks: blocks })
}

function handleSubAgentCreated(eventData: any) {
  const data = eventData.data || eventData
  const taskId = data.taskId || data.agentId
  const pipelineId = data.pipelineId
  const parentId = data.parentId
  if (!taskId || !pipelineId) return

  const ownerSession = eventData._threadId || useSessionStore.getState().activeSessionId
  if (ownerSession) {
    registerPipelineOwner(pipelineId, ownerSession)
  }

  const tabId = `sub-${parentId || taskId}`
  useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)
}

export function initStreamingEvents(): void {
  if (_initialized) return
  _initialized = true

  _handlers[WS_SERVER_EVENTS.STREAM_START] = handleStreamStart
  _handlers[WS_SERVER_EVENTS.STREAM_CHUNK] = handleStreamChunk
  _handlers[WS_SERVER_EVENTS.STREAM_END] = handleStreamEnd
  _handlers[WS_SERVER_EVENTS.NEW_MESSAGE] = handleNewMessage
  _handlers[WS_SERVER_EVENTS.THINKING_START] = handleThinkingStart
  _handlers[WS_SERVER_EVENTS.THINKING_CHUNK] = handleThinkingChunk
  _handlers[WS_SERVER_EVENTS.THINKING_END] = handleThinkingEnd
  _handlers[WS_SERVER_EVENTS.TOOL_START] = handleToolStart
  _handlers[WS_SERVER_EVENTS.TOOL_RESULT] = handleToolResult
  _handlers[WS_SERVER_EVENTS.SUB_AGENT_CREATED] = handleSubAgentCreated

  for (const [event, handler] of Object.entries(_handlers)) {
    wsPool.subscribe(event, handler)
  }
}

export function destroyStreamingEvents(): void {
  if (!_initialized) return
  clearAllChunkTimeouts()
  for (const [event, handler] of Object.entries(_handlers)) {
    wsPool.unsubscribe(event, handler)
  }
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  pipelineSessionMap.clear()
  _initialized = false
}
