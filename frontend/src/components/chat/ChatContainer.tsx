/**
 * 聊天容器组件
 *
 * 整合消息列表、Agent Tab 导航和输入区域的完整聊天界面。
 * 支持 L1/L2/L3 多层 Agent Tab 切换，每个 Tab 独立维护消息列表。
 */

import { Loader2, Search, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import ErrorBoundary from '@/components/ErrorBoundary'
import { useAgentTabStore } from '@/stores/agentTabStore'
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
  isGenerating = false,
  onSendMessage,
  onStopGenerate,
  onRegenerate,
  onEdit,
  onDelete,
  currentTokenUsage = 0,
  maxTokens = 128000,
  modelName = 'glm-5.1',
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
  const tabMessages = useAgentTabStore((s) => s.tabMessages)
  const unreadCounts = useAgentTabStore((s) => s.unreadCounts)
  const switchToTab = useAgentTabStore((s) => s.switchToTab)
  const closeTab = useAgentTabStore((s) => s.closeTab)
  const initSessionTabs = useAgentTabStore((s) => s.initSessionTabs)

  /**
   * 会话切换时初始化 Tab 状态
   *
   * 从 localStorage 恢复该会话的 Tab 配置，
   * 或创建空白状态等待后端推送。
   */
  useEffect(() => {
    if (sessionId) {
      initSessionTabs(sessionId)
    }
  }, [sessionId, initSessionTabs])

  /**
   * 判断是否为子 Tab（L2/L3）激活状态
   *
   * 主 Tab 使用外部传入的 messages，子 Tab 使用 tabMessages 中的数据。
   */
  const activeTab = tabs.find((t) => t.id === activeTabId)
  const isSubTabActive = activeTab != null && activeTab.agentLevel !== 1

  /**
   * BUG-FIX-fix_20260506_per_tab_streaming: 根据 Tab 计算 isGenerating
   *
   * 每个标签页的 streaming 状态独立管理：
   * - 子 Tab：从 streamingStore.streamingTabs[tabId] 获取
   * - 主 Tab：从 streamingStore.streamingTabs['__main__'] 获取，回退到外部传入的 isGenerating
   */
  const streamingTabs = useStreamingStore((s) => s.streamingTabs)
  const effectiveIsGenerating = useMemo(() => {
    if (isSubTabActive && activeTabId) {
      return streamingTabs[activeTabId] ?? false
    }
    return streamingTabs[sessionId] ?? false
  }, [isSubTabActive, activeTabId, streamingTabs, sessionId])

  /**
   * 根据当前激活 Tab 选择消息源
   *
   * - 主 Tab (L1) 或无 Tab：使用外部传入的 messages
   * - 子 Tab (L2/L3)：使用 agentTabStore 中 tabMessages 的数据
   *
   * BUG-FIX-fix_20260506_005: 过滤掉 role=tool 的独立消息
   * 问题根因: 后端返回 type=tool 的记录转为 role=tool 的独立消息，
   *          同时 AI 消息的 toolCalls 也渲染工具卡片，导致重复显示。
   *          tool 消息的工具名、参数、结果等信息显示在消息气泡外面。
   * 修复方案: 过滤掉 role=tool 的消息，工具调用信息已包含在 AI 消息的
   *          toolCalls/contentBlocks 渲染中（ActivityCard 工具卡片）
   */
  const activeMessages = useMemo(() => {
    const source = isSubTabActive && activeTabId
      ? tabMessages[activeTabId] || []
      : messages
    return source.filter((m) => m.role !== 'tool')
  }, [isSubTabActive, activeTabId, tabMessages, messages])

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
      // 搜索消息内容
      if (message.content?.toLowerCase().includes(query)) {
        return true
      }

      // 搜索工具调用名称
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
      className={`flex h-full flex-col ${className}`}
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
        <div className="flex items-center gap-2 px-4 py-2">
          <div className="relative flex-1">
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
            <div className="text-muted-foreground text-sm">
              找到 {filteredMessages.length} 条消息
            </div>
          )}
          {/* 通知中心触发按钮 */}
          <div className="relative">
            <NotificationCenter />
          </div>
        </div>
      </div>

      {/* 消息列表 */}
      <MessageList
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
      <ChatInput
        isGenerating={effectiveIsGenerating}
        onSendMessage={(params) => {
          /**
           * 子 Tab 激活时注入 parentRecordId
           *
           * 当用户在子 Tab（L2/L3）中发送消息时，
           * 将 activeTab 的 parentRecordId 附加到参数中，
           * 以便 WebSocket 消息路由到对应的子管道。
           */
          if (isSubTabActive && activeTab?.parentRecordId) {
            onSendMessage({ ...params, parentRecordId: activeTab.parentRecordId })
          } else {
            onSendMessage(params)
          }
        }}
        onStopGenerate={onStopGenerate}
        placeholder="输入消息，按 Enter 发送..."
        enableThinkingMode={true}
        modelName={modelName}
        currentTokenUsage={currentTokenUsage}
        maxTokens={maxTokens}
        thinkingMode={thinkingMode}
        toggleThinkingMode={toggleThinkingMode}
      />
    </div>
  )
}
