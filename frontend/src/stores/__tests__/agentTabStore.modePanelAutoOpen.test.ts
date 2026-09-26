/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * agentTabStore × 模式面板自动弹出（R92 · D-2）
 *
 * 契约：进入会话对话标签（switchToTab 切换 / initSessionTabs 进会话 /
 * openSubAgentTab setActive 跳入管道对话）时，按该管道 state.mode 自动打开并
 * 激活匹配的模式面板工作区页签；mode 缺失 / 无匹配声明 → 不动作。
 *
 * 测试策略：真实 agentTabStore / queryClient 缓存 / ContributionRegistry /
 * layoutModeStore；仅 pipelineMessageStore mock（外部网络边界，家族共享工厂）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/stores/pipelineMessageStore', async () =>
  (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock())

import { updateSessionsCache } from '@/hooks/queries/useSessionsQuery'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { seedPipelineState } from '@/test/modePanelTestUtils'
import { makeSessionFactory } from './helpers/agentTabTestUtils'
import type { PipelineStateInfo } from '@/services/api/pipelines'
import type { AgentTab } from '@/types/task'

const SESSION_ID = 'sess-nav'
const MAIN_PID = 'pid-main'
const makeSession = makeSessionFactory(SESSION_ID, MAIN_PID)

const mainTab: AgentTab = {
  id: `main-${SESSION_ID}`,
  agentId: 'agentos',
  agentName: '主管道',
  agentLevel: 1,
  pipelineRunId: MAIN_PID,
  path: ['主管道'],
  status: 'running',
  hasUnread: false,
  canClose: false,
  messages: [],
}

function seedModePages(): void {
  contributionRegistry.register({
    type: 'pages', id: 'coding_delivery', title: '编码交付',
    space: 'workspace', slot: 'tab', path: '/p/coding_delivery', mode: 'coding', pluginId: 'mode_coding',
  })
  contributionRegistry.register({
    type: 'pages', id: 'writing_workshop', title: '写作工坊',
    space: 'workspace', slot: 'tab', path: '/p/writing_workshop', mode: 'writing', pluginId: 'mode_writing',
  })
}

describe('agentTabStore — 对话标签激活自动弹出模式面板', () => {
  beforeEach(() => {
    queryClient.removeQueries()
    contributionRegistry.clear()
    localStorage.clear()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    useAgentTabStore.setState({
      tabs: [],
      activeTabId: null,
      tabMessagesLoading: {},
      unreadCounts: {},
      currentSessionId: null,
      pipelineTabMap: {},
    })
    updateSessionsCache(() => [makeSession()])
  })

  it('switchToTab 切到对话 tab：面板页签自动打开并激活', () => {
    seedModePages()
    seedPipelineState(MAIN_PID, { mode: 'coding' })
    useAgentTabStore.setState({ currentSessionId: SESSION_ID, tabs: [mainTab], activeTabId: null })

    useAgentTabStore.getState().switchToTab(mainTab.id)

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-coding_delivery'])
    expect(tabs[0]?.isActive).toBe(true)
  })

  it('mode 不同开不同面板：writing 命中写作工坊而非编码交付', () => {
    seedModePages()
    seedPipelineState(MAIN_PID, { mode: 'writing' })
    useAgentTabStore.setState({ currentSessionId: SESSION_ID, tabs: [mainTab], activeTabId: null })

    useAgentTabStore.getState().switchToTab(mainTab.id)

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-writing_workshop',
    ])
  })

  it('initSessionTabs 进会话：活跃对话 tab 的模式面板自动打开', () => {
    seedModePages()
    seedPipelineState(MAIN_PID, { mode: 'coding' })

    useAgentTabStore.getState().initSessionTabs(SESSION_ID)

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-coding_delivery',
    ])
  })

  it('openSubAgentTab(setActive) 跳入子管道对话：子管道 mode 面板自动打开', () => {
    seedModePages()
    seedPipelineState('pid-sub', { mode: 'coding' })
    useAgentTabStore.setState({ currentSessionId: SESSION_ID, tabs: [mainTab] })

    useAgentTabStore.getState().openSubAgentTab({
      agentId: 'agent-sub',
      agentName: '子Agent',
      parentRecordId: 'rec-1',
      agentLevel: 2,
      setActive: true,
      pipelineId: 'pid-sub',
    })

    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-coding_delivery',
    ])
  })

  it('对话 tab 管道无 mode 键 → 不动作', () => {
    seedModePages()
    seedPipelineState(MAIN_PID, {})
    useAgentTabStore.setState({ currentSessionId: SESSION_ID, tabs: [mainTab], activeTabId: null })

    useAgentTabStore.getState().switchToTab(mainTab.id)

    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })
})
