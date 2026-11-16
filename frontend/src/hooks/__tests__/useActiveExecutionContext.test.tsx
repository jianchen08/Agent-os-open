// @feature: FP-0.2.四 前端 Schema（widget 执行上下文数据面） | @ci: frontend-test
/**
 * useActiveExecutionContext 测试 — 执行上下文读面（ADR 2026-10-10 widget 数据面）
 *
 * 真实 store 直种（zustand 单例 setState，非 mock）：断言三组区分度输入下
 * 的取值口径——Tab 管道坐标 / store 级回退 / 均无空串，及 tab-only 变体
 * 不吃回退的性质（防「回退泄漏进严格口径」的拟合）。
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import {
  readActiveSessionId,
  useActivePipelineId,
  useActiveSessionId,
  useActiveTabPipelineId,
} from '@/hooks/useActiveExecutionContext'
import type { AgentTab } from '@/types/task'

function makeTab(overrides: Partial<AgentTab> & { id: string }): AgentTab {
  return {
    agentId: 'agent-1',
    agentName: '主代理',
    agentLevel: 1,
    path: [],
    status: 'running',
    hasUnread: false,
    ...overrides,
  }
}

describe('useActiveExecutionContext — 管道坐标口径', () => {
  beforeEach(() => {
    useAgentTabStore.setState({ tabs: [], activeTabId: null })
    usePipelineMessageStore.setState({ activePipelineId: null })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('活跃 Tab 带 pipelineRunId → Tab 坐标优先（两组区分度 Tab）', () => {
    const { result } = renderHook(() => useActivePipelineId())
    act(() => {
      useAgentTabStore.setState({
        tabs: [makeTab({ id: 't1', pipelineRunId: 'pipe-tab-1' })],
        activeTabId: 't1',
      })
    })
    expect(result.current).toBe('pipe-tab-1')

    act(() => {
      useAgentTabStore.setState({
        tabs: [makeTab({ id: 't2', pipelineRunId: 'pipe-tab-2' })],
        activeTabId: 't2',
      })
    })
    expect(result.current).toBe('pipe-tab-2')
  })

  it('Tab 无 pipelineRunId → 回退 store 级 activePipelineId；均无 → 空串', () => {
    usePipelineMessageStore.setState({ activePipelineId: 'pipe-store' })
    useAgentTabStore.setState({
      tabs: [makeTab({ id: 't1' })],
      activeTabId: 't1',
    })
    const { result } = renderHook(() => useActivePipelineId())
    expect(result.current).toBe('pipe-store')

    act(() => {
      usePipelineMessageStore.setState({ activePipelineId: null })
    })
    expect(result.current).toBe('')
  })

  it('tab-only 变体不吃 store 级回退（严格跟随标签口径）', () => {
    usePipelineMessageStore.setState({ activePipelineId: 'pipe-store' })
    useAgentTabStore.setState({
      tabs: [makeTab({ id: 't1', pipelineRunId: 'pipe-tab-1' })],
      activeTabId: 't1',
    })
    const { result } = renderHook(() => useActiveTabPipelineId())
    expect(result.current).toBe('pipe-tab-1')

    // 换到无管道坐标的 Tab：即使 store 级有值也返回空串（不串台）
    act(() => {
      useAgentTabStore.setState({
        tabs: [makeTab({ id: 't1', pipelineRunId: 'pipe-tab-1' }), makeTab({ id: 't2' })],
        activeTabId: 't2',
      })
    })
    expect(result.current).toBe('')
  })
})

describe('useActiveExecutionContext — 会话坐标口径', () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: null })
  })

  it('响应式 hook 与非响应式 getter 同源（切换即跟随）', () => {
    const { result } = renderHook(() => useActiveSessionId())
    expect(result.current).toBeNull()
    expect(readActiveSessionId()).toBeNull()

    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-1' })
    })
    expect(result.current).toBe('sess-1')
    expect(readActiveSessionId()).toBe('sess-1')

    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-2' })
    })
    expect(result.current).toBe('sess-2')
    expect(readActiveSessionId()).toBe('sess-2')
  })
})
