/**
 * 聊天容器组件
 *
 * 整合消息列表、Agent Tab 导航和输入区域的完整聊天界面。
 * 支持 L1/L2/L3 多层 Agent Tab 切换，每个 Tab 独立维护消息列表。
 * 每个管道独立获取模型上下文窗口和 token 使用量。
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo } from 'react'
import { useModelContextInfo } from '@/hooks/useModelContextInfo'
import { useAgentStore } from '@/stores/agentStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { useVotingStore } from '@/stores/votingStore'
import { AgentTabBar } from './AgentTabBar'
import { ChatInput } from './ChatInput'
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
 * 将多个连续的 assistant 消息合并为一条，整合 content 和 parts。
 * 不区分流式/非流式，统一合并。组内有 streaming 消息时，合并结果保留 streaming 状态。
 */
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
  isLoading = false,
  isGenerating: _isGenerating = false,
  onSendMessage,
  onStopGenerate,
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
  /** 搜索状态（从 uiStore 共享，Sidebar 中输入） */
  const searchQuery = useUIStore((s) => s.messageSearchQuery)

  /** 从 agentTabStore 获取 Tab 状态 */
  const tabs = useAgentTabStore((s) => s.tabs)
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const unreadCounts = useAgentTabStore((s) => s.unreadCounts)
  const switchToTab = useAgentTabStore((s) => s.switchToTab)
  const closeTab = useAgentTabStore((s) => s.closeTab)
  const initSessionTabs = useAgentTabStore((s) => s.initSessionTabs)

  /** 当前激活 Tab（提前计算，供 pipelineMessages 选择器使用） */
  const activeTab = tabs.find((t) => t.id === activeTabId)

  /**
   * 基于当前激活 Tab 的 agentId 解析模型名
   *
   * 所有管道（主/子）平权处理，统一按当前激活标签的 agent 配置获取模型。
   * 模型随标签切换而变化；查不到时回退到会话级模型名。
   */
  const agents = useAgentStore((s) => s.agents)
  const effectiveModelName = useMemo(() => {
    if (activeTab?.agentId) {
      const agent = agents.find(
        (a) => a.id === activeTab.agentId || a.configId === activeTab.agentId,
      )
      if (agent?.model || agent?.config?.model) {
        return agent.model || agent.config?.model || modelName
      }
    }
    return modelName
  }, [activeTab?.agentId, agents, modelName])

  /**
   * 从 pipelineMessageStore 获取当前激活管道的消息
   *
   * 管道激活统一由 initSessionTabs（会话初始化）和 switchToTab（Tab切换）负责，
   * selector 只需读取 s.activePipelineId，不依赖 activeTab 做回退判断。
   */
  const pipelineMessages = usePipelineMessageStore(
    (s) => {
      if (!s.activePipelineId) return EMPTY_MESSAGES
      const msgs = s.messagesByPipeline[s.activePipelineId] ?? EMPTY_MESSAGES
      return msgs
    },
    (a, b) => {
      if (a === b) return true
      if (!Array.isArray(a) || !Array.isArray(b)) return false
      if (a.length !== b.length) return false
      if (a.length === 0 && b.length === 0) return true
      for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return false
      }
      return true
    },
  )

  /** 会话切换由 setActiveSession 统一处理：fetchMessages + initSessionTabs */
  useEffect(() => {
    // 防御：处理 setActiveSession 之外直接修改 activeSessionId 的场景
    const currentSessionId = useSessionStore.getState().activeSessionId
    if (sessionId && currentSessionId === sessionId) {
      const { activeTabId: currentTabId } = useAgentTabStore.getState()
      if (!currentTabId) {
        initSessionTabs(sessionId)
      }
    }
  }, [sessionId, initSessionTabs])

  /** 判断是否为子 Tab（L2/L3）激活状态 */
  const isSubTabActive = activeTab != null && activeTab.agentLevel !== 1
  const isSubTabFinished = isSubTabActive && (activeTab?.status === 'completed' || activeTab?.status === 'failed')

  /**
   * 当前标签对应管道是否正在流式输出
   *
   * 逻辑：当前标签 → 标签的 pipelineRunId → streamingState[pipelineId].isStreaming
   * 子标签直接用 tab.pipelineRunId，主标签用 pipelineMessageStore.activePipelineId。
   */
  const pipelineActiveId = usePipelineMessageStore((s) => s.activePipelineId)
  const currentTabPipelineId = activeTab?.pipelineRunId || pipelineActiveId
  // BUG-FIX-fix_20260523_max_update_depth:
  // 问题根因: streamingState 是对象，全量订阅导致任何 pipeline 的 streaming 状态变化
  //          都会触发组件重渲染，配合其他 effect 可能产生无限循环。
  // 修复方案: 改为精确选择器，只订阅当前 Tab 对应 pipeline 的 isStreaming 布尔值。
  // 影响范围: ChatContainer 渲染性能，减少不必要的重渲染
  // 修复日期: 2026-05-23
  const effectiveIsGenerating = usePipelineMessageStore(
    (s) => {
      const pid = activeTab?.pipelineRunId || s.activePipelineId
      return pid ? (s.streamingState[pid]?.isStreaming ?? false) : false
    }
  )


  /**
   * 根据当前模型名获取动态 context_window
   */
  const { contextWindow: modelContextWindow } = useModelContextInfo(effectiveModelName)

  /**
   * 从 contextUsageStore 获取当前活跃管道的 token 使用量
   *
   * 每个管道（pipelineId）独立维护自己的 usage 数据。
   */
  const currentPipelineId = currentTabPipelineId || ''
  const pipelineUsage = useContextUsageStore((s) => s.usageByPipeline[currentPipelineId])
  const effectiveTokenUsage = pipelineUsage?.promptTokens ?? 0

  /** 最终的 maxTokens 和 currentTokenUsage */
  const effectiveMaxTokens = modelContextWindow
  const effectiveTokenCount = effectiveTokenUsage

  /**
   * 统一消息源：只使用 pipelineMessageStore
   *
   * 所有消息（流式、API 加载、历史翻页）统一通过 pipelineMessageStore 管理，
   * 不再使用外部传入的 messages prop 作为 fallback，消除双数据源不一致问题。
   */
  const activeMessages = useMemo(() => {
    return pipelineMessages.filter((m: any) => m.role !== 'tool')
  }, [pipelineMessages])

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

      if (message.parts?.some((part) => part.type === 'tool_call' && (part as any).name?.toLowerCase().includes(query))) {
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

      {/* 消息列表 */}
      {/* BUG-FIX-fix_20260509_scroll_position: key 强制切换时重新挂载使 initialTopMostItemIndex 生效 */}
      <MessageList
        key={activeTabId || sessionId}
        tabId={activeTabId || sessionId}
        messages={filteredMessages}
        isGenerating={effectiveIsGenerating}
        modelName={effectiveModelName}
        className="flex-1"
        hasMore={hasMoreMessages}
        isLoadingMore={isLoadingMoreMessages}
        onLoadMore={onLoadMoreMessages}
        sessionId={sessionId}
        searchQuery={searchQuery}
      />

      {/* 子Tab路由增强（无UI，逻辑层） */}
      <SubTabRouter sessionId={sessionId} />

      {/* 活跃投票面板 */}
      <ActiveVotingPanels sessionId={sessionId} />

      {/* 输入区域 + 通知中心 */}
      <div className="relative shrink-0">
        <div className="absolute -top-10 right-2 z-10">
          <NotificationCenter />
        </div>
        {/* BUG-FIX-fix_20260512_input_state_shared: key 强制切换标签时重建 ChatInput，使每个标签的输入状态（text/attachments/pendingFiles）独立 */}
        <ChatInput
          key={`input-${activeTabId || sessionId}`}
          draftKey={activeTabId || sessionId}
          disabled={isSubTabFinished}
          isGenerating={effectiveIsGenerating}
          onSendMessage={(params) => {
            if (isSubTabFinished) return
            // 所有管道（主标签/子标签）一律带 pipelineId，管道ID是唯一路由标识
            const pid = activeTab?.pipelineRunId || pipelineActiveId
            onSendMessage({ ...params, pipelineId: pid })
          }}
          onStopGenerate={onStopGenerate}
          placeholder="输入消息，按 Enter 发送..."
          enableThinkingMode={true}
          modelName={effectiveModelName}
          currentTokenUsage={effectiveTokenCount}
          maxTokens={effectiveMaxTokens}
          completionTokens={pipelineUsage?.completionTokens ?? 0}
          totalTokens={pipelineUsage?.totalTokens ?? 0}
          thinkingMode={thinkingMode}
          toggleThinkingMode={toggleThinkingMode}
        />
      </div>
    </div>
  )
}
