/**
 * 全局流式事件服务
 *
 * 管理所有 WebSocket 流式事件（stream_start/chunk/end、thinking、tool 等）的订阅和处理。
 * 独立于 React 组件生命周期，确保页面切换时流式输出不被中断。
 *
 * 设计目标：
 * - 用户从聊天页导航到其他页面时，流式事件处理器持续运行
 * - 通过 Zustand 全局 store 更新消息和 streaming 状态
 * - 用户返回聊天页时，自动恢复流式输出的显示
 *
 * 使用方式：
 * - init(): WebSocket 连接后调用，注册所有事件处理器
 * - destroy(): 仅在登出或应用关闭时调用，注销所有事件处理器
 */

import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { webSocketService } from '@/services/websocket/WebSocketService'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'

/**
 * chunk 间隔超时时间
 *
 * 检测语义：streaming 开始后，如果连续 N 秒没有收到新的 chunk，
 * 则判定为 chunk 中断（后端管道异常/网络断开），主动终止 streaming 状态。
 */
const CHUNK_INTERVAL_TIMEOUT_MS = 60_000

let _initialized = false

const _handlers: Record<string, (data: any) => void> = {}

/**
 * 按 Tab 独立管理的 chunk 间隔超时定时器
 *
 * key: targetTabId（子 Tab 的 tabId 或 '__main__'）
 * value: { timer, messageId }
 */
interface ChunkTimeoutEntry {
  timer: ReturnType<typeof setTimeout>
  messageId: string
}
const _chunkTimeoutMap: Map<string, ChunkTimeoutEntry> = new Map()

/**
 * 为指定 Tab 启动/重置 chunk 间隔超时定时器
 *
 * 每收到一个 stream_start 或 stream_chunk 时调用，
 * 如果对应 Tab 已有定时器则重置（说明 chunk 持续到达，连接正常）。
 */
function resetChunkTimeout(
  targetTabId: string,
  messageId: string,
  pipelineTabId: string | null,
): void {
  clearChunkTimeout(targetTabId)

  const timer = setTimeout(() => {
    _chunkTimeoutMap.delete(targetTabId)
    const currentSid = useSessionStore.getState().activeSessionId
    const { stopStreamingForTab } = useStreamingStore.getState()
    stopStreamingForTab(targetTabId)

    if (currentSid) {
      if (pipelineTabId) {
        updateSubTabMessage(pipelineTabId, messageId, {
          content: '\n\n⚠️ 流式响应中断，请重试。',
          status: 'error',
        })
      } else {
        const { updateMessageFields } = useSessionStore.getState()
        updateMessageFields(currentSid, messageId, {
          content: '\n\n⚠️ 流式响应中断，请重试。',
          status: 'error',
        } as any)
      }
    }
  }, CHUNK_INTERVAL_TIMEOUT_MS)

  _chunkTimeoutMap.set(targetTabId, { timer, messageId })
}

/**
 * 清除指定 Tab 的 chunk 间隔超时定时器
 */
function clearChunkTimeout(targetTabId: string): void {
  const entry = _chunkTimeoutMap.get(targetTabId)
  if (entry) {
    clearTimeout(entry.timer)
    _chunkTimeoutMap.delete(targetTabId)
  }
}

/**
 * 清除所有 Tab 的 chunk 间隔超时定时器
 */
function clearAllChunkTimeouts(): void {
  for (const [, entry] of _chunkTimeoutMap) {
    clearTimeout(entry.timer)
  }
  _chunkTimeoutMap.clear()
}

/**
 * 从事件数据中提取 pipeline_id 并查找对应的子 Tab ID
 */
function resolvePipelineTab(eventData: any): string | null {
  const pipelineId = eventData.pipeline_id || eventData.data?.pipeline_id
  if (!pipelineId) return null
  return useAgentTabStore.getState().getTabIdByPipeline(pipelineId) ?? null
}

/**
 * 获取子 Tab 中指定 ID 的消息
 */
function getSubTabMessage(tabId: string, messageId: string): any | undefined {
  const tabMsgs = useAgentTabStore.getState().tabMessages[tabId] || []
  return tabMsgs.find((m) => m.id === messageId)
}

/**
 * 更新子 Tab 中的消息（读取-修改-写回）
 */
function updateSubTabMessage(tabId: string, messageId: string, updates: Record<string, any>): void {
  const agentTabStore = useAgentTabStore.getState()
  const tabMsgs = agentTabStore.tabMessages[tabId] || []
  const msg = tabMsgs.find((m) => m.id === messageId)
  if (msg) {
    agentTabStore.addMessageToTab(tabId, { ...msg, ...updates })
  }
}

/**
 * 处理流式开始事件
 *
 * 创建一条空的 assistant 消息占位，后续 chunk 会逐步填充内容。
 * 初始化 contentBlocks 为空数组，由后续事件按到达顺序追加。
 */
function handleStreamStart(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return

  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const pipelineTabId = resolvePipelineTab(eventData)
  const targetTabId = pipelineTabId || '__main__'

  const { setStreamingForTab } = useStreamingStore.getState()
  const { addMessage } = useSessionStore.getState()

  resetChunkTimeout(targetTabId, messageId, pipelineTabId)

  const msgs = useSessionStore.getState().messages[sid] || []
  const nextSeq = msgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1

  if (pipelineTabId) {
    setStreamingForTab(pipelineTabId, true)
    useAgentTabStore.getState().addMessageToTab(pipelineTabId, {
      id: messageId,
      sessionId: sid,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: nextSeq,
      status: 'streaming',
      contentBlocks: [],
    })
    return
  }

  setStreamingForTab('__main__', true)
  addMessage(sid, {
    id: messageId,
    sessionId: sid,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: nextSeq,
    status: 'streaming',
    contentBlocks: [],
  })
}

/**
 * 处理流式内容块事件
 *
 * 将增量内容追加到对应的 assistant 消息。
 * 如果消息处于 thinking 状态，内容路由到 thinking 字段；
 * 否则追加到 content 字段，同时更新 contentBlocks。
 */
function handleStreamChunk(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return

  const messageId = eventData.message_id || eventData.data?.message_id
  const content = eventData.content || eventData.data?.content || ''
  if (!messageId) return

  const { stopStreamingForTab } = useStreamingStore.getState()
  const { updateMessageFields } = useSessionStore.getState()

  const pipelineTabId = resolvePipelineTab(eventData)
  const targetTabId = pipelineTabId || '__main__'
  resetChunkTimeout(targetTabId, messageId, pipelineTabId)

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)

    if (msg?.thinking?.isThinking) {
      const prevContent = msg.thinking.content || ''
      const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
      const lastActiveThinkingIdx = prevBlocks.findLastIndex(
        (b) => b.type === 'thinking' && b.thinking?.isThinking,
      )
      if (lastActiveThinkingIdx !== -1) {
        const block = { ...prevBlocks[lastActiveThinkingIdx] }
        block.thinking = {
          content: (block.thinking?.content || '') + content,
          isThinking: true,
        }
        prevBlocks[lastActiveThinkingIdx] = block
      }
      updateSubTabMessage(pipelineTabId, messageId, {
        thinking: { content: prevContent + content, isThinking: true },
        contentBlocks: prevBlocks,
      })
      return
    }

    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    const lastBlock = prevBlocks[prevBlocks.length - 1]
    if (lastBlock?.type === 'text') {
      prevBlocks[prevBlocks.length - 1] = {
        ...lastBlock,
        text: (lastBlock.text || '') + content,
      }
    } else {
      prevBlocks.push({
        type: 'text',
        text: content,
        sourceId: messageId,
      })
    }

    updateSubTabMessage(pipelineTabId, messageId, {
      contentBlocks: prevBlocks,
      content: (msg?.content || '') + content,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)

  if (msg?.thinking?.isThinking) {
    const prevContent = msg.thinking.content || ''
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    const lastActiveThinkingIdx = prevBlocks.findLastIndex(
      (b) => b.type === 'thinking' && b.thinking?.isThinking,
    )
    if (lastActiveThinkingIdx !== -1) {
      const block = { ...prevBlocks[lastActiveThinkingIdx] }
      block.thinking = {
        content: (block.thinking?.content || '') + content,
        isThinking: true,
      }
      prevBlocks[lastActiveThinkingIdx] = block
    }
    updateMessageFields(sid, messageId, {
      thinking: { content: prevContent + content, isThinking: true },
      contentBlocks: prevBlocks,
    })
    return
  }

  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  const lastBlock = prevBlocks[prevBlocks.length - 1]
  if (lastBlock?.type === 'text') {
    prevBlocks[prevBlocks.length - 1] = {
      ...lastBlock,
      text: (lastBlock.text || '') + content,
    }
  } else {
    prevBlocks.push({
      type: 'text',
      text: content,
      sourceId: messageId,
    })
  }

  const oldContent = msg?.content || ''
  updateMessageFields(sid, messageId, {
    contentBlocks: prevBlocks,
    content: oldContent + content,
  } as any)
}

/**
 * 处理流式结束事件
 *
 * 使用 full_content 校正内容并重建 contentBlocks
 */
function handleStreamEnd(eventData: any) {
  const pipelineTabId = resolvePipelineTab(eventData)
  const targetTabId = pipelineTabId || '__main__'
  clearChunkTimeout(targetTabId)

  const { stopStreamingForTab } = useStreamingStore.getState()
  if (pipelineTabId) {
    stopStreamingForTab(pipelineTabId)
  } else {
    stopStreamingForTab('__main__')
  }

  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return

  const messageId = eventData?.message_id || eventData?.data?.message_id
  const fullContent = eventData?.full_content || eventData?.data?.full_content

  const { updateMessageFields } = useSessionStore.getState()

  if (messageId) {
    if (pipelineTabId) {
      const msg = getSubTabMessage(pipelineTabId, messageId)
      const finalContent = fullContent || msg?.content || ''
      const finalThinking = msg?.thinking
        ? { ...msg.thinking, isThinking: false }
        : undefined
      const reconciledBlocks = reconcileContentBlocks(
        msg?.contentBlocks,
        finalContent,
        msg?.toolCalls,
        finalThinking,
        messageId,
      )
      updateSubTabMessage(pipelineTabId, messageId, {
        status: 'completed',
        content: finalContent,
        contentBlocks: reconciledBlocks,
        _reconciled: true,
        ...(finalThinking ? { thinking: finalThinking } : {}),
      })
    } else {
      const msgs = useSessionStore.getState().messages[sid] || []
      const msg = msgs.find((m) => m.id === messageId)
      const finalContent = fullContent || msg?.content || ''
      const finalThinking = msg?.thinking
        ? { ...msg.thinking, isThinking: false }
        : undefined
      const reconciledBlocks = reconcileContentBlocks(
        msg?.contentBlocks,
        finalContent,
        msg?.toolCalls,
        finalThinking,
        messageId,
      )
      updateMessageFields(sid, messageId, {
        status: 'completed',
        content: finalContent,
        contentBlocks: reconciledBlocks,
        _reconciled: true,
        ...(finalThinking ? { thinking: finalThinking } : {}),
      } as any)
    }
  } else {
    const sessionMessages = useSessionStore.getState().messages[sid] || []
    const needsUpdate = sessionMessages.some(
      (m) => m.status === 'streaming' || m.thinking?.isThinking,
    )
    if (needsUpdate) {
      useStreamingStore.getState().stopStreaming()
    }
  }
}

/**
 * 处理新消息事件
 *
 * 收到完整的最终消息，确保流式状态结束，清除超时定时器。
 * 增加竞态防护：如果 stream_end 已标记 _reconciled，只确认 status。
 */
function handleNewMessage(eventData: any) {
  const pipelineTabId = resolvePipelineTab(eventData)
  const targetTabId = pipelineTabId || '__main__'
  clearChunkTimeout(targetTabId)

  const { stopStreamingForTab } = useStreamingStore.getState()
  if (pipelineTabId) {
    stopStreamingForTab(pipelineTabId)
  } else {
    stopStreamingForTab('__main__')
  }

  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return

  const messageId = eventData?.message_id || eventData?.message?.id || eventData?.data?.message_id || eventData?.data?.id
  const { updateMessageFields } = useSessionStore.getState()

  if (messageId) {
    const finalContent = eventData?.content || eventData?.data?.content

    if (pipelineTabId) {
      const tabMsgs = useAgentTabStore.getState().tabMessages[pipelineTabId] || []
      const existingMsg = tabMsgs.find((m: any) => m.id === messageId)

      if (existingMsg && (existingMsg as any)._reconciled) {
        updateSubTabMessage(pipelineTabId, messageId, { status: 'completed' })
      } else if (existingMsg && finalContent) {
        const finalThinking = existingMsg.thinking
          ? { ...existingMsg.thinking, isThinking: false }
          : undefined
        const reconciledBlocks = reconcileContentBlocks(
          existingMsg.contentBlocks,
          finalContent,
          existingMsg.toolCalls,
          finalThinking,
          messageId,
        )
        updateSubTabMessage(pipelineTabId, messageId, {
          status: 'completed',
          content: finalContent,
          contentBlocks: reconciledBlocks,
          _reconciled: true,
          ...(finalThinking ? { thinking: finalThinking } : {}),
        })
      } else if (existingMsg) {
        updateSubTabMessage(pipelineTabId, messageId, { status: 'completed' })
      }
    } else {
      const sessionMessages = useSessionStore.getState().messages[sid] || []
      const existingMsg = sessionMessages.find((m: any) => m.id === messageId)

      if (existingMsg && (existingMsg as any)._reconciled) {
        updateMessageFields(sid, messageId, { status: 'completed' } as any)
      } else if (existingMsg && finalContent) {
        const finalThinking = existingMsg.thinking
          ? { ...existingMsg.thinking, isThinking: false }
          : undefined
        const reconciledBlocks = reconcileContentBlocks(
          existingMsg.contentBlocks,
          finalContent,
          existingMsg.toolCalls,
          finalThinking,
          messageId,
        )
        updateMessageFields(sid, messageId, {
          status: 'completed',
          content: finalContent,
          contentBlocks: reconciledBlocks,
          _reconciled: true,
          ...(finalThinking ? { thinking: finalThinking } : {}),
        } as any)
      } else {
        updateMessageFields(sid, messageId, {
          status: 'completed',
        } as any)
      }
    }
  }
}

/**
 * 处理思考开始事件
 */
function handleThinkingStart(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const pipelineTabId = resolvePipelineTab(eventData)
  const { updateMessageFields } = useSessionStore.getState()

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    prevBlocks.push({
      type: 'thinking',
      thinking: { content: '', isThinking: true },
      sourceId: messageId,
    })
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: '', isThinking: true },
      contentBlocks: prevBlocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)
  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  prevBlocks.push({
    type: 'thinking',
    thinking: { content: '', isThinking: true },
    sourceId: messageId,
  })

  updateMessageFields(sid, messageId, {
    thinking: { content: '', isThinking: true },
    contentBlocks: prevBlocks,
  } as any)
}

/**
 * 处理思考内容块事件
 */
function handleThinkingChunk(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const chunk = eventData.content || eventData.data?.content || ''
  if (!messageId || !chunk) return

  const pipelineTabId = resolvePipelineTab(eventData)
  const { updateMessageFields } = useSessionStore.getState()

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const prevContent = msg?.thinking?.content || ''
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    const lastActiveThinkingIdx = prevBlocks.findLastIndex(
      (b) => b.type === 'thinking' && b.thinking?.isThinking,
    )
    if (lastActiveThinkingIdx !== -1) {
      const block = { ...prevBlocks[lastActiveThinkingIdx] }
      block.thinking = {
        content: (block.thinking?.content || '') + chunk,
        isThinking: true,
      }
      prevBlocks[lastActiveThinkingIdx] = block
    }
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: prevContent + chunk, isThinking: true },
      contentBlocks: prevBlocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)
  const prevContent = msg?.thinking?.content || ''

  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  const lastActiveThinkingIdx = prevBlocks.findLastIndex(
    (b) => b.type === 'thinking' && b.thinking?.isThinking,
  )
  if (lastActiveThinkingIdx !== -1) {
    const block = { ...prevBlocks[lastActiveThinkingIdx] }
    block.thinking = {
      content: (block.thinking?.content || '') + chunk,
      isThinking: true,
    }
    prevBlocks[lastActiveThinkingIdx] = block
  }

  updateMessageFields(sid, messageId, {
    thinking: { content: prevContent + chunk, isThinking: true },
    contentBlocks: prevBlocks,
  } as any)
}

/**
 * 处理思考结束事件
 */
function handleThinkingEnd(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return
  const messageId = eventData.message_id || eventData.data?.message_id
  if (!messageId) return

  const pipelineTabId = resolvePipelineTab(eventData)
  const { updateMessageFields } = useSessionStore.getState()

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    const lastActiveThinkingIdx = prevBlocks.findLastIndex(
      (b) => b.type === 'thinking' && b.thinking?.isThinking,
    )
    if (lastActiveThinkingIdx !== -1) {
      const block = { ...prevBlocks[lastActiveThinkingIdx] }
      block.thinking = {
        content: block.thinking?.content || '',
        isThinking: false,
      }
      prevBlocks[lastActiveThinkingIdx] = block
    }
    updateSubTabMessage(pipelineTabId, messageId, {
      thinking: { content: msg?.thinking?.content || '', isThinking: false },
      contentBlocks: prevBlocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)

  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  const lastActiveThinkingIdx = prevBlocks.findLastIndex(
    (b) => b.type === 'thinking' && b.thinking?.isThinking,
  )
  if (lastActiveThinkingIdx !== -1) {
    const block = { ...prevBlocks[lastActiveThinkingIdx] }
    block.thinking = {
      content: block.thinking?.content || '',
      isThinking: false,
    }
    prevBlocks[lastActiveThinkingIdx] = block
  }

  updateMessageFields(sid, messageId, {
    thinking: { content: msg?.thinking?.content || '', isThinking: false },
    contentBlocks: prevBlocks,
  } as any)
}

/**
 * 处理工具调用开始事件
 */
function handleToolStart(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id || `call_${Date.now()}`
  const newToolCall = {
    call_id: callId,
    tool_name: toolName,
    tool_args: eventData.args || eventData.data?.args || {},
    status: 'running' as const,
    started_at: new Date().toISOString(),
  }

  const pipelineTabId = resolvePipelineTab(eventData)
  const { updateMessageFields } = useSessionStore.getState()

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const existing = msg?.toolCalls || []
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    prevBlocks.push({
      type: 'tool_call',
      toolCall: newToolCall,
      sourceId: messageId,
    })
    updateSubTabMessage(pipelineTabId, messageId, {
      toolCalls: [...existing, newToolCall],
      contentBlocks: prevBlocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)
  const existing = msg?.toolCalls || []

  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  prevBlocks.push({
    type: 'tool_call',
    toolCall: newToolCall,
    sourceId: messageId,
  })

  updateMessageFields(sid, messageId, {
    toolCalls: [...existing, newToolCall],
    contentBlocks: prevBlocks,
  } as any)
}

/**
 * 处理工具调用结果事件
 */
function handleToolResult(eventData: any) {
  const sid = useSessionStore.getState().activeSessionId
  if (!sid) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id

  const buildUpdatedToolCalls = (existing: any[]) => {
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
        call_id: callId || `call_${Date.now()}`,
        tool_name: toolName,
        tool_args: {},
        status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
        result: eventData.result ?? eventData.data?.result,
        completed_at: new Date().toISOString(),
        duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
      })
    }
    return updated
  }

  const updateBlocksForResult = (prevBlocks: any[], updated: any[]) => {
    for (let i = 0; i < prevBlocks.length; i++) {
      const block = prevBlocks[i]
      if (block.type === 'tool_call' && block.toolCall?.status === 'running') {
        const matchedUpdate = updated.find(
          (tc) => tc.call_id === block.toolCall!.call_id && tc.status !== 'running',
        )
        if (matchedUpdate) {
          prevBlocks[i] = { ...block, toolCall: matchedUpdate }
        }
      }
    }
    return prevBlocks
  }

  const pipelineTabId = resolvePipelineTab(eventData)
  const { updateMessageFields } = useSessionStore.getState()

  if (pipelineTabId) {
    const msg = getSubTabMessage(pipelineTabId, messageId)
    const existing = msg?.toolCalls || []
    const updated = buildUpdatedToolCalls(existing)
    const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
    updateBlocksForResult(prevBlocks, updated)
    updateSubTabMessage(pipelineTabId, messageId, {
      toolCalls: updated,
      contentBlocks: prevBlocks,
    })
    return
  }

  const msgs = useSessionStore.getState().messages[sid] || []
  const msg = msgs.find((m) => m.id === messageId)
  const existing = msg?.toolCalls || []
  const updated = buildUpdatedToolCalls(existing)

  const prevBlocks = msg?.contentBlocks ? [...msg.contentBlocks] : []
  updateBlocksForResult(prevBlocks, updated)

  updateMessageFields(sid, messageId, {
    toolCalls: updated,
    contentBlocks: prevBlocks,
  } as any)
}

/**
 * 处理子 Agent 创建事件
 *
 * 仅注册 pipeline_id → tabId 映射，不自动打开子标签。
 */
function handleSubAgentCreated(eventData: any) {
  const data = eventData.data || eventData
  const taskId = data.taskId || data.agentId
  const pipelineId = data.pipelineId
  const parentId = data.parentId

  if (!taskId || !pipelineId) return

  const tabId = `sub-${parentId || taskId}`
  useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)
}

/**
 * 初始化全局流式事件处理器
 *
 * 订阅所有 WebSocket 流式事件（stream_start/chunk/end、thinking、tool 等）。
 * 处理器通过 Zustand store 直接更新状态，不依赖任何 React 组件。
 * 应在 WebSocket 连接成功后调用，且仅调用一次。
 */
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
    webSocketService.subscribe(event, handler)
  }
}

/**
 * 销毁全局流式事件处理器
 *
 * 注销所有 WebSocket 事件订阅并清理超时定时器。
 * 仅在用户登出或应用关闭时调用。
 */
export function destroyStreamingEvents(): void {
  if (!_initialized) return

  clearAllChunkTimeouts()

  for (const [event, handler] of Object.entries(_handlers)) {
    webSocketService.unsubscribe(event, handler)
  }

  _handlers.length = 0
  Object.keys(_handlers).forEach((k) => delete _handlers[k])
  _initialized = false
}
