/** 聊天容器组件 整合消息列表、Agent Tab 导航和输入区域的完整聊天界面。 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useShallow } from 'zustand/react/shallow'
import { Loader2 } from '@/assets/icons'
import { pipelineStatusToTabStatus } from '@/components/schema/widgets/pipelineStatusVisuals'
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import { useLlmConfigQuery, useThinkingLevelsQuery } from '@/hooks/queries/useLlmQueries'
import { usePipelineRunsQuery } from '@/hooks/queries/usePipelineRunsQuery'
import { forceReloadSessions, readSessions } from '@/hooks/queries/useSessionsQuery'
import { useSessionThemeScope } from '@/hooks/useSessionThemeScope'
import { type LLMDefaults } from '@/services/api/config'
import { syncModeThemeForSession } from '@/services/modeSessionBinder'
import { syncPluginStylesForSession } from '@/services/pluginStyles'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useModelSelectStore, useExplicitModelSelect } from '@/stores/modelSelectStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import {
  useThinkingModeStore,
  useExplicitThinkingStrength,
} from '@/stores/thinkingModeStore'
import { useUIStore } from '@/stores/uiStore'
import { useVotingStore } from '@/stores/votingStore'
import { type ThinkingStrength } from '@/types/thinkingMode'
import { mainPipelineIdOf } from '@/utils/mappers'
import { resolveModelDisplayName } from '@/utils/modelName'
import { AgentTabBar } from './AgentTabBar'
import { ChatFloatingLayer } from './ChatFloatingLayer'
import { ChatInput } from './ChatInput'
import { MessageList } from './MessageList'
import { PendingInputQueueBar } from './PendingInputQueueBar'
import { ReferenceSelectionRow } from './ReferenceSelectionRow'
import { VotingPanel } from './VotingPanel'
import type { ChatContainerProps } from './types'
import type { Message } from '@/types/models'
import type { PipelineRunInfo } from '@/types/pipeline'

const EMPTY_MESSAGES: Message[] = []

/**
 * 一次性降级提示（S4）：同一 key 整个应用生命周期只弹一次通知，
 * 避免重复失败（轮询重试/多 Tab 切换）刷屏；失败本身仍可从控制台看到。
 */
const degradedNotifiedKeys = new Set<string>()
function notifyDegradedOnce(
  key: string,
  payload: { title: string; message: string; priority?: 'low' | 'normal' | 'high' },
): void {
  if (degradedNotifiedKeys.has(key)) return
  degradedNotifiedKeys.add(key)
  const { priority = 'normal', ...rest } = payload
  useNotificationStore.getState().addNotification({
    ...rest,
    priority,
    category: 'alert',
    isBlocking: false,
    autoDismissMs: 6000,
  })
}

/** 合并连续的 assistant 消息 将多个连续的 assistant 消息合并为一条，整合 content 和 parts。 */
/** 活跃投票面板列表 从 votingStore 获取当前会话的活跃投票并渲染。 */
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

/** 主标签名回落：主标签以主管道标题为名（agentTabStore.buildMainTab 建立，
 *  改名经 sessionListStore.renameSession 同步），仅当标签名缺失时兜底显示
 *  agent 名「灵汐」（内部词汇不进用户可见面）。 */
const MAIN_TAB_NAME = '灵汐'

/** 管道标题尚未加载时的标签标题降级名（注册表无行/无 title 键）。
 *  不得用 agentName 顶替：那是 agent/工具侧词汇（交互标题、node.agent_name、
 *  config_id 等来源混入该字段），占标题位即把工具名当任务名显示。 */
const UNNAMED_TAB_NAME = '未命名任务'

/** 将 agentTabStore 中的 Tab 数据映射为 AgentTabBar 组件所需格式 */
function mapStoreTabsToBarFormat(
  storeTabs: ReturnType<typeof useAgentTabStore.getState>['tabs'],
  activeTabId: string | null,
  unreadCounts: Record<string, number>,
  runsSnapshot: Record<string, PipelineRunInfo>,
) {
  return storeTabs.map((tab) => {
    // 标签标题 = 管道标题（注册表 run.title，ADR 2026-10-27-pipeline-title-field
    // 单字段真值；与任务管理面板、TaskSceneEntryButton 同源同口径）。runs 快照
    // 随 30s 轮询 + WS 增量刷新，标签名因此跟随管道出生/改名对账，不再冻结在
    // 开签时写入的 agentName 快照上（子 Tab 此前无任何回写路径，一次错永久错）。
    // 管道标题缺失：主标签回落 agent 名「灵汐」（既有命名契约），子标签落
    // 「未命名任务」——agentName 只行进悬浮提示（AgentTabItem 的 title=path）。
    const pipelineTitle = tab.pipelineRunId ? runsSnapshot[tab.pipelineRunId]?.title : undefined
    const name = pipelineTitle
      ? pipelineTitle
      : tab.agentLevel === 1
        ? tab.agentName || MAIN_TAB_NAME
        : UNNAMED_TAB_NAME
    // 标签状态对齐权威 run 态：store 快照只在开签时写一次、之后无人回写
    // （管道恢复运行后标签仍显示旧终态 ✕，与任务管理页不一致——2026-11-07
    // 用户实证）。runs 快照 = 任务管理页同源数据（stream_start/end/error
    // 增量 + 30s 兜底轮询），有行即以它投影；无行保持快照（缺数据不猜）。
    const runStatus = tab.pipelineRunId ? runsSnapshot[tab.pipelineRunId]?.status : undefined
    return {
      id: tab.id,
      name,
      status: runStatus ? pipelineStatusToTabStatus(runStatus) : tab.status,
      isActive: tab.id === activeTabId,
      unreadCount: unreadCounts[tab.id] || 0,
      canClose: tab.canClose,
      agentLevel: tab.agentLevel,
      agentName: name,
      taskId: tab.taskId,
      path: tab.path,
    }
  })
}

/** 聊天容器组件 */
export const ChatContainer = ({
  sessionId,
  isLoading = false,
  onSendMessage,
  onStopGenerate,
  onForceSendPending,
  className = '',
  hasMoreMessages = false,
  isLoadingMoreMessages = false,
  onLoadMoreMessages,
  onRegenerate,
  onRollbackTo,
  onEdit,
}: ChatContainerProps) => {
  /** 搜索状态（从 uiStore 共享，Sidebar 中输入） */
  const searchQuery = useUIStore((s) => s.messageSearchQuery)
  /** 待定位消息（Sidebar 消息命中点击写入；仅当目标管道正是当前激活管道时消费） */
  const messageJump = useUIStore((s) => s.messageJump)
  const setMessageJump = useUIStore((s) => s.setMessageJump)

  /** 从 agentTabStore 获取 Tab 状态 */
  const tabs = useAgentTabStore((s) => s.tabs)
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const unreadCounts = useAgentTabStore((s) => s.unreadCounts)
  const switchToTab = useAgentTabStore((s) => s.switchToTab)
  const closeTab = useAgentTabStore((s) => s.closeTab)
  const initSessionTabs = useAgentTabStore((s) => s.initSessionTabs)

  /** 当前激活 Tab（提前计算，供 pipelineMessages 选择器使用） */
  const activeTab = tabs.find((t) => t.id === activeTabId)

  /** 基于当前激活 Tab 解析模型名 所有管道（主/子）平权处理，统一按当前标签的 agent 配置获取模型。 */
  const { data: agents = [] } = useAgentsQuery()
  /** 子管道对应的 agent config_id（来自 pipelineMeta.agentName，sub_agent_created 事件下发） */
  const pipelineAgentName = usePipelineMessageStore((s) => {
    const pid = activeTab?.pipelineRunId
    return pid ? s.pipelines[pid]?.agentName ?? '' : ''
  })

  /**
   * LLM 配置（共享 query 缓存，queryKeys.llmConfig）：模型显示 tiers 与思考
   * 强度反向映射同源消费。挂载期一次性快照（useEffect+useState）会在设置页
   * 改配置后过期——输入框兜底模型名随缓存更新热更新（2026-09-28 批次 C）。
   * 失败降级：tiers 缺失时 resolveModelDisplayName 原样返回键名，models 缺失
   * 时思考强度回退默认档位，一次性提示。
   */
  const { data: llmConfig, isError: llmConfigError } = useLlmConfigQuery()
  const llmDefaults: LLMDefaults | null = llmConfig?.defaults ?? null
  useEffect(() => {
    if (!llmConfigError) return
    notifyDegradedOnce('chat-llm-config', {
      title: '模型配置获取失败',
      message: '模型列表与思考档位暂不可用，已按默认配置显示',
    })
  }, [llmConfigError])

  const rawModelName = useMemo(() => {
    const candidateIds = [activeTab?.agentId, pipelineAgentName].filter(Boolean) as string[]
    for (const id of candidateIds) {
      const agent = agents.find(
        (a) => a.id === id || a.configId === id,
      )
      if (agent?.model || agent?.config?.model) {
        return agent.model || agent.config?.model || ''
      }
    }
    return ''
  }, [activeTab?.agentId, pipelineAgentName, agents])

  /** P8: 分级键（large/medium/small）→ 具体模型名；具体模型名原样返回。
   *  agent 头未配置 model（rawModelName 空）→ 兜底系统默认对话模型
   *  （llm.yaml defaults.chat，注册表键形态）；llm 配置整体缺失 → 空串
   *  （选择器空态占位，不发明模型名）。 */
  const effectiveModelName = useMemo(
    () => resolveModelDisplayName(rawModelName || llmDefaults?.chat || '', llmDefaults?.tiers),
    [rawModelName, llmDefaults],
  )

  /** 从 pipelineMessageStore 获取当前激活管道的消息 管道激活统一由 initSessionTabs（会话初始化）和 switchToTab（Tab切换）负责。
   *  单一消息数组：乐观 user/流式 assistant/已确认消息全在
   *  messagesByPipeline 同一数组，按 status 状态机区分生命周期——渲染层零拼接。 */
  /** 当前激活管道（消息流真实数据面）：会话主标签没有单一 taskId 时用它兜底 */
  const sceneTaskId = usePipelineMessageStore((st) => st.activePipelineId)

  const pipelineMessages = usePipelineMessageStore(
    // useShallow：快照每次都是新引用（数组元素级稳定），zustand v5 useSyncExternalStore
    // 会因引用不稳触发 forceStoreRerender 无限循环（e2e 实测 Maximum update
    // depth exceeded）——浅比较元素级稳定即可。
    useShallow((s) => {
      if (!s.activePipelineId) return EMPTY_MESSAGES
      return s.messagesByPipeline[s.activePipelineId] ?? EMPTY_MESSAGES
    }),
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

  /** 模式主题随会话激活（批 G⑤）：会话 modeBinding 的模式声明 theme 非空 →
   *  会话主题 override 幂等补同步（出生已入栈；重载后栈丢失场景在此恢复；
   *  无 mode/无声明/registry 不可达零动作）。异步取数 fire-and-forget。 */
  useEffect(() => {
    void syncModeThemeForSession(sessionId)
  }, [sessionId])

  /** 插件 CSS 会话域门重同步（ADR 2026-10-14-message-card-body-form）：带 modes
   *  声明的 client_styles 随活跃会话 modeBinding 进出生效域（如模式会话换装流式
   *  光标，切走即移除）；无 modes 声明不受影响（既有全局装饰语义零变化）。 */
  useEffect(() => {
    syncPluginStylesForSession(contributionRegistry.getClientStyles(), sessionId)
  }, [sessionId])

  /** 判断是否为子 Tab（L2/L3）激活状态 */
  const isSubTabActive = activeTab != null && activeTab.agentLevel !== 1
  const isSubTabFinished = isSubTabActive && (activeTab?.status === 'completed' || activeTab?.status === 'failed')

  /** 当前标签对应管道是否正在流式输出。
   *  双来源收紧：只用 activeTab.pipelineRunId 单一来源（与发送侧
   *  ChatInput 同款正例）——不再 fallback store 级 activePipelineId，Tab 数据损坏
   *  时宁可不显示生成态也不串到别的管道。
   *  runs 快照兜底（用户裁定 2026-09-11 恢复正常逻辑）：刷新页面/重进会话时
   *  流式事件不会重放，run 若在工具执行间隙（无实时 chunk）会隐形运行——
   *  生成态再并上 runs 快照的 running 真值（30s 轮询 + WS 增量同源），
   *  停止按钮/输入框禁用据此恢复。 */
  const currentTabPipelineId = activeTab?.pipelineRunId || ''
  const { data: runsSnapshot = {} } = usePipelineRunsQuery()
  // 轮次收尾快照：run 结束但链尾 drain 还要继续消费（queue_depth>0）→ 仍算
  // 运行中（pipeline_round_finished 携带队列深度，前端不再提前回到空闲）。
  const roundQueueDepth = usePipelineMessageStore((s) =>
    currentTabPipelineId ? (s.roundQueueDepthByPipeline?.[currentTabPipelineId] ?? 0) : 0,
  )
  // 停止受理中：点停即置位（router.handleStopGenerate），run 级终止事件/下一轮
  // stream_start 清除；护栏 TTL（streaming handlers utils STOP_REQUEST_TTL_MS）
  // 过期仍无终态事件时由其定时翻转解除。
  const stoppingActive = usePipelineMessageStore((s) =>
    currentTabPipelineId ? (s.stoppingByPipeline?.[currentTabPipelineId] ?? false) : false,
  )
  // 生成态单一真值：runs 快照（stream_start 实时写 running）+ 收尾快照。
  // 原 streamingActive 双源已删——stream_start 已同步 runs cache，重复订阅只会
  // 制造两套状态不一致。
  // stopping 终态守卫：终止事件丢失时 runs 轮询到达终态也要解除「停止中」，
  // 不能靠 pipeline_round_finished 单通道（断线重放溢出即永久停止中）。
  const runStatus = runsSnapshot[currentTabPipelineId]?.status
  const stoppingLive =
    stoppingActive &&
    runStatus !== 'completed' &&
    runStatus !== 'failed' &&
    runStatus !== 'cancelled'
  // 点停即时反馈（2026-11-07）：停止受理期间生成观感立即让位——转圈停、发送
  // 按钮恢复可用，不等后端 round_finished（收尾链秒级延迟曾造成「点了没反应」）；
  // 正文冻结由 handleStopGenerate 的 stopStreaming 落地。护栏 TTL 过期 stopping
  // 被翻转解除后，生成观感回落 runs 真值：run 仍在跑则转圈恢复（停止未生效
  // 可辨别、可再点），不静默装作已停。
  const effectiveIsGenerating = stoppingLive
    ? false
    : runStatus === 'running' || roundQueueDepth > 0

  /** 发送在途：当前标签管道有已受理未落定的发送（chatInputStore 登记，气泡认领/
   *  队列条出现/5s 落定超时/失败判死四口撤下，2026-11-04 落定语义重定义）→
   *  发送按钮转圈、输入锁定 */
  const sendInFlight = useChatInputStore((s) =>
    currentTabPipelineId ? (s.sendInFlightByPipeline[currentTabPipelineId]?.length ?? 0) > 0 : false,
  )

  /** 等待 AI 响应壳（ADR 2026-11-17-awaiting-reply-render-shell）：直跑 user 气泡
   *  已落定、AI 真气泡（stream_start）未出现的窗口锚点。流式中/排队中（队列条
   *  是排队期唯一视图）不亮壳——三口清除在 store（startStreaming/stopStreaming/
   *  TTL），此处只收敛「不与既有视图抢位」两条件。 */
  const streamingActive = usePipelineMessageStore((s) =>
    currentTabPipelineId ? (s.streamingState?.[currentTabPipelineId]?.isStreaming ?? false) : false,
  )
  const awaitingReplySinceRaw = usePipelineMessageStore((s) =>
    currentTabPipelineId ? (s.awaitingReplyByPipeline?.[currentTabPipelineId] ?? 0) : 0,
  )
  const awaitingReplySince =
    !streamingActive && roundQueueDepth === 0 ? awaitingReplySinceRaw : 0

  /**
   * 待定位消息 → MessageList 消费目标：仅当跳转目标管道正是当前激活管道时透传。
   * 目标未命中当前管道（跨会话/子管道跳转的中间态）不透传，等 Tab/管道切换到位后
   * 本 memo 重新求值再消费——jumpTarget 常驻 uiStore 直到被消费。
   */
  const effectiveJumpTarget = useMemo(
    () => (messageJump && messageJump.pipelineId === currentTabPipelineId ? messageJump : undefined),
    [messageJump, currentTabPipelineId],
  )


  /** 思考选择：显式记忆（∈ 下发选项时生效）?? 端点当前参数组（反向推断
   *  真值源在后端 current，前端零推断零映射）。 */
  const explicitThinkingStrength = useExplicitThinkingStrength()
  const { data: thinkingLevelsData } = useThinkingLevelsQuery(effectiveModelName)
  const activeThinkingStrength: ThinkingStrength = useMemo(() => {
    if (
      explicitThinkingStrength &&
      thinkingLevelsData?.options.some((o) => o.value === explicitThinkingStrength)
    ) {
      return explicitThinkingStrength
    }
    return thinkingLevelsData?.current ?? ''
  }, [explicitThinkingStrength, thinkingLevelsData])
  const setThinkingStrength = useThinkingModeStore((s) => s.setStrength)

  /** 显式模型选择（标签级记忆）：'' = 未选择（帧不带 model_id，跟随配置）。
   *  随消息透传 → 内核按 widget 声明 frame_field 名单合成 state overlay →
   *  llm_core 解析链首位（最高优先级覆盖管道配置）。 */
  const explicitModelId = useExplicitModelSelect()
  const setModelId = useModelSelectStore((s) => s.setModel)
  const handleModelIdChange = useCallback(
    (modelIdValue: string) => {
      const tabId = useAgentTabStore.getState().activeTabId
      if (tabId) setModelId(tabId, modelIdValue)
    },
    [setModelId],
  )

  /**
   * 思考选择路由（llm_core 按 thinking_strength_params 解析）：选择记入标签级
   * 记忆（localStorage，随路由联动），随消息透传落管道持久 state——零后端参谋
   * 调用；重发/回退不重传参数，管道按回退后的状态正常跑时自动继承。
   */
  const handleThinkingStrengthChange = useCallback(
    (strength: ThinkingStrength) => {
      const tabId = useAgentTabStore.getState().activeTabId
      if (tabId) setThinkingStrength(tabId, strength)
    },
    [setThinkingStrength],
  )

  /**
   * 统一消息源：保留所有消息（含 tool）——渲染层 MessageList 的
   * mergeConsecutiveAssistantMessages 需要 tool 消息来把结果注入
   * assistant 的 tool_call part，在此过滤会导致子管道消息大量丢失。
   */
  const activeMessages = pipelineMessages

  /** 将 store Tab 映射为 AgentTabBar 所需格式（状态以 runs 权威态实时投影） */
  const barTabs = useMemo(
    () => mapStoreTabsToBarFormat(tabs, activeTabId, unreadCounts, runsSnapshot),
    [tabs, activeTabId, unreadCounts, runsSnapshot],
  )

  /** 是否显示 AgentTabBar（至少存在一个 Tab 时显示） */
  // 单主对话标签也显示（顶部中间恒有标签条）
  const showTabBar = tabs.length >= 1
  // 顶带槽位（FiveSpaceLayout 顶带的标签行容器）：存在=portal 进拖拽容器，
  // 不存在（移动端/无顶带形态）=内联回退。整树提交后查询，时序必然就绪。
  const [tabsSlot, setTabsSlot] = useState<HTMLElement | null>(null)
  useLayoutEffect(() => {
    setTabsSlot(document.getElementById('chat-top-band-tabs'))
  }, [])

  /** 处理 Tab 切换 */
  const handleTabChange = useCallback(
    (tabId: string) => {
      switchToTab(tabId)
    },
    [switchToTab],
  )

  /** 处理 Tab 关闭 */
  const handleTabClose = useCallback(
    (tabId: string) => {
      closeTab(tabId)
    },
    [closeTab],
  )

  /**
   * 标签栏节点（portal 与内联回退共用同一份，避免两处参数漂移）：
   * 能力集与工作区标签栏对齐——切换/关闭/固定/拖拽换位/右键菜单（含批量关签
   * 确认层）/激活签滚动可见；组件内部直接复用 components/layout 的共用实现，
   * 此处只把 agentTabStore 通道接上。
   */
  const tabBarNode = (
    <AgentTabBar
      tabs={barTabs}
      onTabChange={handleTabChange}
      onTabClose={handleTabClose}
      onReorder={(dragId, targetId) => useAgentTabStore.getState().reorderTab(dragId, targetId)}
      onCloseOtherTabs={(tabId) => useAgentTabStore.getState().closeOtherTabs(tabId)}
      onCloseAllTabs={() => useAgentTabStore.getState().closeAllTabs()}
      onTogglePin={(tabId) => useAgentTabStore.getState().togglePinTab(tabId)}
    />
  )

  /** 过滤消息 */
  const filteredMessages = useMemo(() => {
    if (!searchQuery.trim()) {
      return activeMessages
    }

    const query = searchQuery.toLowerCase()
    return activeMessages.filter((message) => {
      if (message.content?.toLowerCase().includes(query)) {
        return true
      }

      if (message.parts?.some((part) => part.type === 'tool_call' && part.name.toLowerCase().includes(query))) {
        return true
      }

      return false
    })
  }, [activeMessages, searchQuery])

  /** 会话主题 override 作用域挂点（主题桥 §5.0：作用域仅聊天区+面板容器） */
  const chatThemeScopeRef = useSessionThemeScope<HTMLDivElement>()

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
      ref={chatThemeScopeRef}
      // 聊天区背景由 .theme-chat-area 类消费（--chat-bg-image/-color 双通道）：
      // bg-[var(--chat-bg)] 生成 background-color 位，渐变主题（deep-space 等）
      // 塞进去会整条失效→聊天区透明→全屏纹理层穿透内容区
      className={`theme-chat-area relative flex h-full min-h-0 flex-col overflow-hidden ${className}`}
      data-testid="chat-container"
      data-session-id={sessionId}
      // 皮肤垂坠带让位（skinRuntime 写列级变量）：图形花边悬在让位留白上，
      // 不压顶带标签行/消息列顶/输入卡底；无皮肤时变量恒 0 零影响
      data-skin-inset-target="chat"
      style={{
        paddingTop: 'var(--skin-chat-inset-top, 0px)',
        paddingBottom: 'var(--skin-chat-inset-bottom, 0px)',
      }}
      // 聊天容器状态（我方词汇）：empty=空态欢迎页 / active=对话态。
      // DSH 皮肤的 [data-phase=hero/active] 由适配器递送层转译到这里
      // （位置映射，不给组件贴 DSH 名字）
      data-chat-state={filteredMessages.length > 0 ? 'active' : 'empty'}
    >
      {/* 通用悬浮挂载槽（2026-10-24 用户裁定：位置按插件声明 anchor 定位，
          本槽零业务知识；无 anchor 声明时零渲染） */}
      {/* 任务现场入口已下沉到「最新 AI 气泡下方的状态行」（TaskSceneStatusRow，
            用户裁定 2026-11-20）：消息流是主入口，右上角浮动入口退役。
            悬浮槽保留——插件声明 anchor 的挂件仍走这里，本槽零业务知识。 */}
        <ChatFloatingLayer />
      {/* 对话标签行：桌面 portal 进布局顶带槽位（chat-top-band-tabs）——
          顶带是唯一拖拽容器，标签行作为其 DOM 子元素自动获得 no-drag 洞；
          槽位不存在（移动端/无顶带形态）回退内联渲染。 */}
      {showTabBar && tabsSlot &&
        createPortal(
          <div className="flex h-full min-w-0 items-center justify-center">{tabBarNode}</div>,
          tabsSlot,
        )}
      {showTabBar && !tabsSlot && (
        <div
          className="flex shrink-0 items-center justify-center"
          style={{
            // 高度=内容行 40px + 安全区：固定 h-10 配 border-box 时状态栏
            // 内边距会吃掉内容盒（负高度），flex 居中把标签 chip 顶进状态栏带
            height: 'calc(2.5rem + var(--safe-area-top, 0px))',
            paddingTop: 'var(--safe-area-top, 0px)',
          }}
          data-testid="chat-session-header"
        >
          <div className="min-w-0">{tabBarNode}</div>
        </div>
      )}

      {/* 消息列表 */}
      {/* key 强制切换时重新挂载使 initialTopMostItemIndex 生效 */}
      <MessageList
        key={activeTabId || sessionId}
        tabId={activeTabId || sessionId}
        messages={filteredMessages}
        isGenerating={effectiveIsGenerating}
        className="flex-1"
        hasMore={hasMoreMessages}
        isLoadingMore={isLoadingMoreMessages}
        onLoadMore={onLoadMoreMessages}
        sessionId={sessionId}
        searchQuery={searchQuery}
        // 任务坐标兜底：会话标签可能只有 pipelineRunId（无 taskId）——按「任务=管道」
        // 同源口径回落，否则最新气泡状态行的任务现场入口会因空坐标整块不渲染
        // （2026-11-20 真实数据实测：entry=0、data-task-id=""）
        taskId={
          activeTab?.taskId ||
          currentTabPipelineId ||
          sceneTaskId ||
          undefined
        }
        onEdit={onEdit}
        onRegenerate={onRegenerate}
        onRollbackTo={onRollbackTo}
        awaitingReplySince={awaitingReplySince}
        jumpTarget={effectiveJumpTarget}
        onJumpConsumed={() => setMessageJump(null)}
      />

      {/* 活跃投票面板 */}
      <ActiveVotingPanels sessionId={sessionId} />

      {/* 输入区域 · Deep Space v2: 上边框 + 12px 内边距（P5: 通知中心已移至侧边栏唯一入口） */}
      <div
        className="relative shrink-0 border-t px-3 py-3"
        style={{
          borderColor: 'var(--ds-border-subtle)',
          // 全屏沉浸下内容铺到手势条底下，输入区自行避让（桌面变量=0 不变）
          paddingBottom: 'max(0.75rem, var(--safe-area-bottom, 0px))',
        }}
        data-testid="chat-composer"
      >
        {/* 输入区与消息列消费同一对话列宽（--chat-content-max-w）：宽视口下
            输入框与上方消息同轴居中；窄于上限（移动端/分屏）零变化。 */}
        <div className="mx-auto w-full max-w-[var(--chat-content-max-w)]">
          {/* 引用选中镜像行（实时镜像：选中出现 / 取消消失；选中非空发送时
              provider 域随消息注入引用；经注册缝渲染，壳不感知具体插件域）。
              threadId 必须用 sessionId：内核 WS 绑定按 activeSessionId 注册
              （sendActiveThread），用 activeTabId（main-<sessionId>）订阅会导致
              事件单播查无绑定被丢弃——引用只随刷新显示。 */}
          <ReferenceSelectionRow threadId={sessionId} />
          {/* 待处理输入队列条（ADR-2026-08-26）：执行中发送的消息在此排队，
              点击条目退回输入框/删除/清空/立即发送；消费激活后进主消息流 */}
          {currentTabPipelineId && (
            <PendingInputQueueBar
              pipelineId={currentTabPipelineId}
              onForceSend={
                onForceSendPending
                  ? (inputId) => onForceSendPending(currentTabPipelineId, inputId)
                  : undefined
              }
            />
          )}
          {/* key 强制切换标签时重建 ChatInput，使每个标签的输入状态（text/attachments/pendingFiles）独立 */}
          <ChatInput
            key={`input-${activeTabId || sessionId}`}
            draftKey={activeTabId || sessionId}
            disabled={isSubTabFinished}
            isGenerating={effectiveIsGenerating}
            sendInFlight={sendInFlight}
            onSendMessage={(params) => {
              if (isSubTabFinished) return false
              // 管道 ID 单一来源：当前标签的 pipelineRunId（主标签=后端回填的主管道
              // ID，子标签=sub_agent_created 事件下发的子管道 ID）。
              // 主标签兜底：对话框预建会话在缓存重拉/事件回填竞态下 pipelineRunId
              // 可能短暂为空——回退到会话主管道映射解析（pipelineIds[0]），
              // 发送不再静默丢失；子标签不兜底（串管道风险）。
              let pid = activeTab?.pipelineRunId
              if (!pid && !isSubTabActive) {
                const session = readSessions().find((s) => s.id === sessionId)
                pid = (session && mainPipelineIdOf(session)) || ''
              }
              if (!pid) {
                // 未受理显式提示 + 返回 false：ChatInput 保留输入与草稿（不再静默丢消息）
                if (isSubTabActive) {
                  notifyDegradedOnce('chat-subtab-send-unsupported', {
                    title: '子任务标签不支持发送',
                    message: '请回到会话第一个标签发送消息',
                  })
                } else {
                  // 自愈（BUG-28）：主管道解析失败多为会话缓存陈旧/未就绪（列表拉取
                  // 失败、创建响应与列表重拉的竞态窗口）——强制重拉会话列表拿到权威
                  // 主管道后重建标签面，用户重试即可受理；不绕过受理协议硬发（无主管
                  // 道时硬发会把消息丢进空管道）。重拉失败一次性显式提示，不静默吞错。
                  void forceReloadSessions()
                    .then(() => {
                      // 用户已切走会话时不回写旧会话标签面（陈旧异步不得扰动当前视图）
                      if (useAgentTabStore.getState().currentSessionId === sessionId) {
                        initSessionTabs(sessionId)
                      }
                    })
                    .catch((error) => {
                      console.error('[ChatContainer] 会话列表强制重拉失败:', error)
                      notifyDegradedOnce('chat-sessions-reload-failed', {
                        title: '会话列表刷新失败',
                        message: '会话数据暂不可用，请检查连接后重试',
                      })
                    })
                  useNotificationStore.getState().addNotification({
                    title: '会话未就绪',
                    message: '会话尚未就绪，请稍候重试',
                    priority: 'normal',
                    category: 'error',
                    isBlocking: false,
                    autoDismissMs: 6000,
                    sourceLabel: '系统',
                  })
                }
                return false
              }
              return onSendMessage({ ...params, pipelineId: pid })
            }}
            onStopGenerate={onStopGenerate}
            enableThinkingMode={true}
            modelName={effectiveModelName}
            thinkingStrength={activeThinkingStrength}
            onThinkingStrengthChange={handleThinkingStrengthChange}
            modelId={explicitModelId}
            onModelIdChange={handleModelIdChange}
          />
        </div>
      </div>
    </div>
  )
}
