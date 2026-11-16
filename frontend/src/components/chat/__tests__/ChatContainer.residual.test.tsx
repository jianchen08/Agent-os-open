/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ChatContainer 装配面残余分支：
 * - 标签行重排（onReorder）两条渲染路径：顶带 portal 槽位存在 / 槽位缺失的内联回退
 * - 模型选择透传：ChatInput 的选择回调按当前激活标签写入标签级记忆
 * - 待处理队列的「立即发送」：仅当宿主提供 onForceSendPending 才下发强制发送回调
 * - 主管道解析失败的自愈：强制重拉会话列表后重建标签面（用户未切走的会话）
 *
 * mock 边界：子视图（标签条/消息列/输入区/队列条）以契约桩承接（本文件验证
 * 装配与透传，不重测子视图内部）；查询网络源（会话/智能体/管道/LLM）打桩。
 */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useModelSelectStore } from '@/stores/modelSelectStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { ChatContainer } from '../ChatContainer'
import type { SendMessageParams } from '../types'
import type { AgentTab } from '@/types/task'

const stubs = vi.hoisted(() => ({
  tabBar: null as null | {
    onReorder?: (dragId: string, targetId: string) => void
  },
  chatInput: null as null | {
    onModelIdChange?: (modelId: string) => void
    onSendMessage?: (params: SendMessageParams) => boolean | void
  },
  pendingBar: null as null | {
    pipelineId: string
    onForceSend?: (inputId: string) => void
  },
  sessions: [] as Array<{ id: string; title?: string; pipelineIds?: string[] }>,
  forceReload: vi.fn(),
  initSessionTabs: vi.fn(),
  reorderTab: vi.fn(),
}))

vi.mock('../AgentTabBar', () => ({
  AgentTabBar: (props: NonNullable<typeof stubs.tabBar>) => {
    stubs.tabBar = props
    return <div data-testid="stub-tab-bar" />
  },
}))
vi.mock('../MessageList', () => ({
  MessageList: () => <div data-testid="stub-message-list" />,
}))
vi.mock('../ChatInput', () => ({
  ChatInput: (props: NonNullable<typeof stubs.chatInput>) => {
    stubs.chatInput = props
    return (
      <div data-testid="stub-chat-input">
        <button
          data-testid="stub-model-change"
          onClick={() => props.onModelIdChange?.('provider/model-z')}
        >
          换模型
        </button>
        <button
          data-testid="stub-send"
          onClick={() => props.onSendMessage?.({ content: '在吗' })}
        >
          发送
        </button>
      </div>
    )
  },
}))
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => <div data-testid="stub-reference-row" />,
}))
vi.mock('../PendingInputQueueBar', () => ({
  PendingInputQueueBar: (props: NonNullable<typeof stubs.pendingBar>) => {
    stubs.pendingBar = props
    return (
      <button data-testid="stub-force-send" onClick={() => props.onForceSend?.('in-9')}>
        立即发送
      </button>
    )
  },
}))
vi.mock('../VotingPanel', () => ({
  VotingPanel: () => <div data-testid="stub-voting-panel" />,
}))
vi.mock('../ChatFloatingLayer', () => ({
  ChatFloatingLayer: () => null,
}))

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => stubs.sessions,
  forceReloadSessions: () => stubs.forceReload(),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: () => ({ data: {} }),
}))
vi.mock('@/hooks/queries/useLlmQueries', () => ({
  useLlmConfigQuery: () => ({ data: null, isError: false }),
  useThinkingLevelsQuery: () => ({ data: null }),
}))
vi.mock('@/hooks/useSessionThemeScope', () => ({
  useSessionThemeScope: () => ({ current: null }),
}))
vi.mock('@/services/modeSessionBinder', () => ({
  syncModeThemeForSession: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/services/pluginStyles', () => ({
  syncPluginStylesForSession: vi.fn(),
}))
vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getClientStyles: () => [] },
}))
vi.mock('@/stores/agentTabStore', async () => {
  const { create } = await import('zustand')
  const useAgentTabStore = create<Record<string, unknown>>(() => ({
    tabs: [],
    activeTabId: null,
    unreadCounts: {},
    currentSessionId: null,
    switchToTab: vi.fn(),
    closeTab: vi.fn(),
    initSessionTabs: (...args: unknown[]) => stubs.initSessionTabs(...args),
    reorderTab: (...args: unknown[]) => stubs.reorderTab(...args),
  }))
  return { useAgentTabStore }
})

function makeMainTab(overrides: Partial<AgentTab> = {}): AgentTab {
  return {
    id: 'main-1',
    agentId: '',
    agentName: '灵汐',
    agentLevel: 1,
    pipelineRunId: 'pipe-1',
    path: [],
    status: 'running',
    hasUnread: false,
    canClose: false,
    ...overrides,
  }
}

function seedTabs(tabs: AgentTab[], activeTabId: string | null) {
  useAgentTabStore.setState({ tabs, activeTabId, unreadCounts: {} })
}

const baseProps = {
  sessionId: 'sess-1',
  onSendMessage: vi.fn(() => true),
}

beforeEach(() => {
  document.body.innerHTML = ''
  stubs.tabBar = null
  stubs.chatInput = null
  stubs.pendingBar = null
  stubs.sessions = []
  stubs.forceReload.mockReset().mockResolvedValue([])
  stubs.initSessionTabs.mockReset()
  stubs.reorderTab.mockReset()
  localStorage.clear()
  useModelSelectStore.setState({ modelByTabId: {} } as never)
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    activePipelineId: null,
    streamingState: {},
    agentNamesByPipeline: {},
  } as never)
  useSessionStore.setState({ activeSessionId: 'sess-1' } as never)
  useUIStore.setState({ messageSearchQuery: '', messageJump: null } as never)
  seedTabs([makeMainTab()], 'main-1')
  useAgentTabStore.setState({ currentSessionId: 'sess-1' })
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ChatContainer — 标签行重排透传', () => {
  it('顶带槽位存在（portal 路径）→ 重排回调落到标签 store', () => {
    const slot = document.createElement('div')
    slot.id = 'chat-top-band-tabs'
    document.body.appendChild(slot)

    render(<ChatContainer {...baseProps} />)

    act(() => stubs.tabBar?.onReorder?.('main-1', 'main-2'))
    expect(stubs.reorderTab).toHaveBeenCalledWith('main-1', 'main-2')
  })

  it('顶带槽位缺失（内联回退路径）→ 重排回调同样落到标签 store', () => {
    render(<ChatContainer {...baseProps} />)

    expect(screen.getByTestId('chat-session-header')).toBeInTheDocument()
    act(() => stubs.tabBar?.onReorder?.('main-2', 'main-1'))
    expect(stubs.reorderTab).toHaveBeenCalledWith('main-2', 'main-1')
  })
})

describe('ChatContainer — 模型选择与立即发送透传', () => {
  it('输入区换模型 → 记入当前激活标签（标签级记忆）', () => {
    render(<ChatContainer {...baseProps} />)

    act(() => stubs.chatInput?.onModelIdChange?.('provider/model-z'))

    expect(useModelSelectStore.getState().modelByTabId['main-1']).toBe('provider/model-z')
  })

  it('宿主提供强制发送回调时，「立即发送」按当前管道下发', () => {
    const onForceSendPending = vi.fn()
    render(<ChatContainer {...baseProps} onForceSendPending={onForceSendPending} />)

    expect(stubs.pendingBar?.pipelineId).toBe('pipe-1')
    act(() => stubs.pendingBar?.onForceSend?.('in-9'))

    expect(onForceSendPending).toHaveBeenCalledWith('pipe-1', 'in-9')
  })
})

describe('ChatContainer — 主管道解析失败的自愈', () => {
  it('当前会话未切走 → 强制重拉后重建标签面并拒绝本次发送（保留输入）', async () => {
    seedTabs([makeMainTab({ pipelineRunId: '' })], 'main-1')
    // 会话快照无主管道可解析 → 走自愈分支
    stubs.sessions = [{ id: 'sess-1', title: '会话', pipelineIds: [] }]

    const onSendMessage = vi.fn(() => true)
    render(<ChatContainer {...baseProps} onSendMessage={onSendMessage} />)

    await act(async () => {
      stubs.chatInput?.onSendMessage?.({ content: '在吗' })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(stubs.forceReload).toHaveBeenCalledTimes(1)
    expect(stubs.initSessionTabs).toHaveBeenCalledWith('sess-1')
    expect(onSendMessage).not.toHaveBeenCalled()
  })
})
