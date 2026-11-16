// @feature FP-T12 前端组件补测
/**
 * 功能测试：会话主题 override 的聊天区消费面（卡主题出生钉的对偶面）
 *
 * 卡主题随扮演会话出生钉入 sessionThemeStore 后，若聊天区不消费会话 override，
 * 出生钉等于对偶零生效（用户症状：聊天气泡仍是全局主题的描边）。本测试钉住
 * 端到端可见面：出生钉入栈 → 活跃会话切换到该会话 → 聊天区根元素带上卡档编译
 * 出的气泡变量（MessageItem 的用户气泡边框读 --bubble-user-border）。
 *
 * 子组件以 stub 替身参与（ChatContainer 既有测试缝同款）：断言只落在渲染出的
 * DOM 变量上，不断言作用域内部实现。
 */
import { render, act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { ChatContainer } from '../ChatContainer'
import type { AgentTab } from '@/types/task'

vi.mock('../ChatInput', () => ({
  ChatInput: () => <div data-testid="stub-chat-input" />,
}))
vi.mock('../MessageList', () => ({
  MessageList: () => <div data-testid="stub-message-list" />,
}))
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => <div data-testid="stub-reference-row" />,
}))
vi.mock('../PendingInputQueueBar', () => ({
  PendingInputQueueBar: () => <div data-testid="stub-pending-bar" />,
}))
vi.mock('../VotingPanel', () => ({
  VotingPanel: () => <div data-testid="stub-voting-panel" />,
}))
vi.mock('../AgentTabBar', () => ({
  AgentTabBar: () => <div data-testid="stub-tab-bar" />,
}))

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  readSessions: () => [{ id: 'sess-rp', activePipelineId: 'pipe-1', pipelineIds: ['pipe-1'] }],
  forceReloadSessions: () => Promise.resolve([]),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: () => ({ data: {} }),
}))
vi.mock('@/hooks/queries/useLlmQueries', () => ({
  useLlmConfigQuery: () => ({ data: undefined, isError: false }),
  useThinkingLevelsQuery: () => ({ data: null }),
}))

const MAIN_TAB: AgentTab = {
  id: 'main-1',
  agentId: '',
  agentName: '',
  agentLevel: 1,
  pipelineRunId: 'pipe-run-1',
  path: [],
  status: 'running',
  hasUnread: false,
  canClose: false,
}

describe('ChatContainer 消费会话主题 override（卡主题出生钉的对偶面）', () => {
  beforeEach(() => {
    useAgentTabStore.setState({ tabs: [MAIN_TAB], activeTabId: 'main-1', unreadCounts: {} })
    useSessionThemeStore.setState({ stacks: {} })
    useSessionStore.setState({ activeSessionId: 'sess-rp' })
  })

  it('卡主题入该会话 override 栈 → 聊天区根元素带该档气泡变量；切离该会话即卸除', () => {
    const cardTheme = presetThemes['pixel-art']
    act(() => {
      useSessionThemeStore.getState().pushTheme('sess-rp', cardTheme)
    })

    const { getByTestId } = render(<ChatContainer sessionId="sess-rp" onSendMessage={vi.fn()} />)
    const root = getByTestId('chat-container')

    // 用户气泡描边变量随会话 override 走（气泡边框读此变量，非全局主题）
    expect(root.style.getPropertyValue('--bubble-user-border')).toBe(
      cardTheme.colors.bubble.user_border,
    )

    // 切到无 override 的会话 → 变量卸除（回默认主题，无跨档残留）
    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-other' })
    })
    expect(root.style.getPropertyValue('--bubble-user-border')).toBe('')
  })

  it('无会话 override → 聊天区不挂任何卡主题变量（键缺席=现役零变化）', () => {
    const { getByTestId } = render(<ChatContainer sessionId="sess-rp" onSendMessage={vi.fn()} />)

    expect(getByTestId('chat-container').style.getPropertyValue('--bubble-user-border')).toBe('')
  })
})
