/**
 * 聊天容器组件
 *
 * 整合消息列表、Agent Tab 导航和输入区域的完整聊天界面。
 * 支持 L1/L2/L3 多层 Agent Tab 切换，每个 Tab 独立维护消息列表。
 * 每个管道独立获取模型上下文窗口和 token 使用量。
 */

import { Loader2, Search, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { buildContentBlocksFromMessage } from '@/components/chat/hooks/useMessageRender'
import ErrorBoundary from '@/components/ErrorBoundary'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useModelContextInfo } from '@/hooks/useModelContextInfo'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { useVotingStore } from '@/stores/votingStore'
import { AgentTabBar } from './AgentTabBar'
import { ChatInput } from './ChatInput'
import { InteractionPanel } from './InteractionPanel'
import { MessageList } from './MessageList'
import { NotificationCenter } from './NotificationCenter'
import { SubTabRouter } from './SubTabRouter'
import { VotingPanel } from './VotingPanel'
import type { ChatContainerProps } from './types'
import type { Message } from '@/types/models'

const EMPTY_MESSAGES: Message[] = []

/**
 * 合并连续的 assistant 消息
 *
 * 将多个连续的 assistant 消息合并为一条，整合 content、thinking、toolCalls 和 contentBlocks。
 * 单条 assistant 消息若缺少 contentBlocks，也会自动补建。
 */
function mergeConsecutiveAssistantMessages(messages: Message[]): Message[] {
  if (messages.length <= 1) return messages
  const result: Message[] = []
  let i = 0
  while (i < messages.length) {
    const msg = messages[i]
    if (msg.role !== 'assistant') {
      result.push(msg)
      i++
      continue
    }
    const groupStart = i
    while (i < messages.length && messages[i].role === 'assistant') { i++ }
    const group = messages.slice(groupStart, i)
    if (group.length === 1) {
      const single = group[0]
      if (!single.contentBlocks || single.contentBlocks.length === 0) {
        result.push({
          ...single,
          contentBlocks: buildContentBlocksFromMessage(single.content, single.toolCalls, single.thinking, single.id),
        })
      } else {
        // BUG-FIX-fix_20260510_tool_display_streaming: 创建新对象引用，确保 Virtuoso 检测到变化
        // 问题根因: 单条 assistant 消息有 contentBlocks 时直接 push 原引用，在 Virtuoso 虚拟滚动环境中，
        //          即使 pipelineMessageStore.updateMessage 创建了新的消息对象，但 mergeConsecutiveAssistantMessages
        //          的 useMemo 可能因 pipelineMessages 引用比较不敏感而返回缓存的旧数组（含旧对象引用），
        //          导致下游组件无法检测到 toolCalls/contentBlocks 的变化。
        // 修复方案: 始终创建新的消息对象引用，确保 Virtuoso 的 data 数组项是新的引用。
        result.push({ ...single })
      }
      continue
    }
    const first = group[0]
    const allToolCalls: Message['toolCalls'] = []
    const allContent: string[] = []
    const interleavedBlocks: Message['contentBlocks'] = []
    const seenCallIds = new Set<string>()
    for (const m of group) {
      if (m.thinking?.content && m.thinking.content.trim()) {
        interleavedBlocks.push({ type: 'thinking', thinking: { content: m.thinking.content.trim(), isThinking: false }, sourceId: m.id })
      }
      if (m.content && m.content.trim()) {
        allContent.push(m.content.trim())
        interleavedBlocks.push({ type: 'text', text: m.content.trim(), sourceId: m.id })
      }
      if (m.toolCalls && m.toolCalls.length > 0) {
        for (const tc of m.toolCalls) {
          if (tc.call_id && seenCallIds.has(tc.call_id)) continue
          if (tc.call_id) seenCallIds.add(tc.call_id)
          allToolCalls.push(tc)
          interleavedBlocks.push({ type: 'tool_call', toolCall: tc, sourceId: m.id })
        }
      }
    }
    const mergedContent = allContent.join('\n\n')
    const mergedThinking = interleavedBlocks.filter(b => b.type === 'thinking').map(b => (b as any).thinking?.content || '').filter(Boolean).join('\n\n')
    result.push({
      ...first,
      content: mergedContent,
      thinking: mergedThinking ? { content: mergedThinking, isThinking: false } : undefined,
      toolCalls: allToolCalls.length > 0 ? allToolCalls : undefined,
      contentBlocks: interleavedBlocks,
    })
  }
  return result
}

/**
 * 活跃投票面板列表
 *
 * 从 votingStore 获取当前会话的活跃投票并渲染。
 */
function ActiveVotingPanels({ sessionId }: { sessionId: string }) {
  const votingSessions = useVotingStore((s) => s.votingSessions)
  const activeVotings = votingSessions.filter(
    (v) => (v.sessionId === sessionId || !v.sessionId) && v.status === 'open',
  )

  if (activeVotings.length === 0) return null

  return (
    <div className="shrink-0">
      {activeVotings.map((voting) => (
        <VotingPanel key={voting.id} voting={voting} />
      ))}
    </div>
  )
}

/**
 * 将 agentTabStore 中的 Tab 数据映射为 AgentTabBar 组件所需格式
 *
 * @param storeTabs - store 中的 AgentTab 列表
 * @param activeTabId - 当前激活的 Tab ID
 * @param unreadCounts - 未读计数映射
 * @returns AgentTabBar 兼容的 Tab 数据数组
 */
function mapStoreTabsToBarFormat(
  storeTabs: ReturnType<typeof useAgentTabStore.getState>['tabs'],
  activeTabId: string | null,
  unreadCounts: Record<string, number>,
) {
  return storeTabs.map((tab) => ({
    id: tab.id,
    name: tab.agentName,
    status: tab.status,
    isActive: tab.id === activeTabId,
    unreadCount: unreadCounts[tab.id] || 0,
    canClose: tab.canClose,
    agentLevel: tab.agentLevel,
    agentName: tab.agentName,
    taskId: tab.taskId,
    path: tab.path,
  }))
}

/**
 * 聊天容器组件
 */
export const ChatContainer = ({
  sessionId,
  messages,
  isLoading = false,
  isGenerating: _isGenerating = false,
  onSendMessage,
  onStopGenerate,
  onRegenerate,
  onEdit,
  onDelete,
  currentTokenUsage: _externalTokenUsage = 0,
  maxTokens: _externalMaxTokens = 0,
  modelName = '',
  thinkingMode,
  toggleThinkingMode,
  className = '',
  hasMoreMessages = false,
  isLoadingMoreMessages = false,
  onLoadMoreMessages,
}: ChatContainerProps) => {
  /** 搜索状态 */
  const [searchQuery, setSearchQuery] = useState('')

  /** 从 agentTabStore 获取 Tab 状态 */
  const tabs = useAgentTabStore((s) => s.tabs)
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const unreadCounts = useAgentTabStore((s) => s.unreadCounts)
  const switchToTab = useAgentTabStore((s) => s.switchToTab)
  const closeTab = useAgentTabStore((s) => s.closeTab)
  const initSessionTabs = useAgentTabStore((s) => s.initSessionTabs)

  /**
   * 从 pipelineMessageStore 获取当前激活管道的消息
   */
  const pipelineMessages = usePipelineMessageStore((s) => {
    const activeId = s.activePipelineId
    if (!activeId) return EMPTY_MESSAGES
    return s.messagesByPipeline[activeId] ?? EMPTY_MESSAGES
  })

  /**
   * 会话切换时初始化 Tab 状态并激活对应管道
   */
  useEffect(() => {
    if (sessionId) {
      initSessionTabs(sessionId)
      const { activeTabId, tabs } = useAgentTabStore.getState()
      const activeTab = tabs.find((t) => t.id === activeTabId)
      const pipelineStore = usePipelineMessageStore.getState()

      if (activeTab && activeTab.agentLevel !== 1 && activeTab.pipelineRunId) {
        pipelineStore.activatePipeline(activeTab.pipelineRunId)
      } else if (activeTab && activeTab.agentLevel === 1) {
        const sessions = useSessionStore.getState().sessions
        const session = sessions.find(s => s.id === sessionId)
        const mainPipelineId = session?.activePipelineId || ''
        if (mainPipelineId) {
          pipelineStore.activatePipeline(mainPipelineId)
          const existing = pipelineStore.messagesByPipeline[mainPipelineId]
          if (!existing || existing.length === 0) {
            pipelineStore.fetchMessages(mainPipelineId, { threadId: sessionId }).catch(() => {})
          }
        }
      }
    }
  }, [sessionId, initSessionTabs])

  /**
   * 判断是否为子 Tab（L2/L3）激活状态
   */
  const activeTab = tabs.find((t) => t.id === activeTabId)
  const isSubTabActive = activeTab != null && activeTab.agentLevel !== 1

  /**
   * BUG-FIX-fix_20260509_tab_streaming: 根据 pipelineId 计算 isGenerating
   */
  const streamingTabs = useStreamingStore((s) => s.streamingTabs)
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const effectiveIsGenerating = useMemo(() => {
    if (activePipelineId) {
      return streamingTabs[activePipelineId] ?? false
    }
    return false
  }, [streamingTabs, activePipelineId])

  /**
   * 根据当前模型名获取动态 context_window
   */
  const { contextWindow: modelContextWindow } = useModelContextInfo(modelName)

  /**
   * 从 contextUsageStore 获取当前活跃管道的 token 使用量
   *
   * 每个管道（pipelineId）独立维护自己的 usage 数据。
   */
  const currentPipelineId = activePipelineId || ''
  const pipelineUsage = useContextUsageStore((s) => s.usageByPipeline[currentPipelineId])
  const effectiveTokenUsage = pipelineUsage?.promptTokens ?? 0

  /** 最终的 maxTokens 和 currentTokenUsage */
  const effectiveMaxTokens = modelContextWindow
  const effectiveTokenCount = effectiveTokenUsage

  /**
   * 根据当前激活管道获取消息列表
   *
   * BUG-FIX-fix_20260512_msg_disappear:
   * 问题根因: 之前 pipelineMessages 为空时直接用 EMPTY_MESSAGES，
   *          但 messages prop（来自 sessionStore）可能已有数据。
   *          页面刷新后 pipelineMessageStore 异步加载，
   *          在加载完成前用户看到空白聊天界面。
   * 修复方案: pipelineMessages 为空时 fallback 到 messages prop，
   *          确保 sessionStore 先加载的消息也能正常显示。
   *          同时将 messages 加入 useMemo 依赖，确保 prop 更新时重新计算。
   * 影响范围: 聊天消息列表显示
   * 修复日期: 2026-05-12
   */
  const activeMessages = useMemo(() => {
    const source = pipelineMessages.length > 0
      ? pipelineMessages
      : (messages && messages.length > 0 ? messages : EMPTY_MESSAGES)
    const filtered = source.filter((m: any) => m.role !== 'tool')
    return mergeConsecutiveAssistantMessages(filtered)
  }, [pipelineMessages, messages])

  /**
   * 将 store Tab 映射为 AgentTabBar 所需格式
   */
  const barTabs = useMemo(
    () => mapStoreTabsToBarFormat(tabs, activeTabId, unreadCounts),
    [tabs, activeTabId, unreadCounts],
  )

  /** 是否显示 AgentTabBar（至少存在一个 Tab 时显示） */
  const showTabBar = tabs.length > 1

  /**
   * 处理 Tab 切换
   *
   * @param tabId - 目标 Tab ID
   */
  const handleTabChange = useCallback(
    (tabId: string) => {
      switchToTab(tabId)
    },
    [switchToTab],
  )

  /**
   * 处理 Tab 关闭
   *
   * @param tabId - 要关闭的 Tab ID
   */
  const handleTabClose = useCallback(
    (tabId: string) => {
      closeTab(tabId)
    },
    [closeTab],
  )

  /**
   * 过滤消息
   */
  const filteredMessages = useMemo(() => {
    if (!searchQuery.trim()) {
      return activeMessages
    }

    const query = searchQuery.toLowerCase()
    return activeMessages.filter((message) => {
      if (message.content?.toLowerCase().includes(query)) {
        return true
      }

      if (message.toolCalls?.some((tool) => tool.tool_name?.toLowerCase().includes(query))) {
        return true
      }

      return false
    })
  }, [activeMessages, searchQuery])

  /** 加载状态 */
  if (isLoading) {
    return (
      <div
        className={`flex h-full flex-col items-center justify-center ${className}`}
        data-testid="chat-container-loading"
      >
        <Loader2 className="text-primary h-8 w-8 animate-spin" />
        <p className="text-muted-foreground mt-2">加载中...</p>
      </div>
    )
  }

  return (
    <div
      className={`flex h-full min-h-0 flex-col overflow-hidden ${className}`}
      data-testid="chat-container"
      data-session-id={sessionId}
    >
      {/* Agent Tab 导航栏（多 Tab 时显示） */}
      {showTabBar && (
        <div className="bg-background shrink-0 border-b">
          <AgentTabBar
            tabs={barTabs}
            onTabChange={handleTabChange}
            onTabClose={handleTabClose}
          />
        </div>
      )}

      {/* 搜索栏 + 通知中心 */}
      <div className="bg-background shrink-0 border-b">
        <div className="flex items-center gap-2 px-2 py-2 sm:px-4">
          <div className="relative min-w-0 flex-1">
            <Search className="text-muted-foreground absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 transform" />
            <Input
              type="text"
              placeholder="搜索消息内容..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8 pr-8 pl-9 text-sm"
            />
            {searchQuery && (
              <Button
                variant="ghost"
                size="sm"
                className="absolute top-1/2 right-1 h-6 w-6 -translate-y-1/2 transform p-0"
                onClick={() => setSearchQuery('')}
              >
                <X className="h-3 w-3" />
              </Button>
            )}
          </div>
          {searchQuery && (
            <div className="text-muted-foreground shrink-0 text-sm">
              找到 {filteredMessages.length} 条消息
            </div>
          )}
          {/* 通知中心触发按钮 */}
          <div className="relative shrink-0">
            <NotificationCenter />
          </div>
        </div>
      </div>

      {/* 消息列表 */}
      {/* BUG-FIX-fix_20260509_scroll_position: key 强制切换时重新挂载使 initialTopMostItemIndex 生效 */}
      <MessageList
        key={activeTabId || sessionId}
        messages={filteredMessages}
        isGenerating={effectiveIsGenerating}
        onRegenerate={onRegenerate}
        onEdit={onEdit}
        onDelete={onDelete}
        modelName={modelName}
        className="flex-1"
        hasMore={hasMoreMessages}
        isLoadingMore={isLoadingMoreMessages}
        onLoadMore={onLoadMoreMessages}
        sessionId={sessionId}
        searchQuery={searchQuery}
      />

      {/* 子Tab路由增强（无UI，逻辑层） */}
      <SubTabRouter sessionId={sessionId} />

      {/* 人类交互卡片 */}
      <ErrorBoundary>
        <InteractionPanel sessionId={sessionId} />
      </ErrorBoundary>

      {/* 活跃投票面板 */}
      <ActiveVotingPanels sessionId={sessionId} />

      {/* 输入区域 */}
      {/* BUG-FIX-fix_20260512_input_state_shared: key 强制切换标签时重建 ChatInput，使每个标签的输入状态（text/attachments/pendingFiles）独立 */}
      <ChatInput
        key={`input-${activeTabId || sessionId}`}
        draftKey={activeTabId || sessionId}
        isGenerating={effectiveIsGenerating}
        onSendMessage={(params) => {
          /**
           * 子 Tab 激活时注入 pipelineId（即 tab.pipelineRunId）
           * 后端直接用 pipeline_id 路由到对应管道，无需 task_service 查找
           */
          if (isSubTabActive && activeTab?.pipelineRunId) {
            onSendMessage({ ...params, pipelineId: activeTab.pipelineRunId })
          } else {
            onSendMessage(params)
          }
        }}
        onStopGenerate={onStopGenerate}
        placeholder="输入消息，按 Enter 发送..."
        enableThinkingMode={true}
        modelName={modelName}
        currentTokenUsage={effectiveTokenCount}
        maxTokens={effectiveMaxTokens}
        thinkingMode={thinkingMode}
        toggleThinkingMode={toggleThinkingMode}
      />
    </div>
  )
}
