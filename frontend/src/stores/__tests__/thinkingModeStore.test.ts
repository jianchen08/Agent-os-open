/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：thinkingModeStore 标签级思考强度记忆 + 路由联动
 *
 * 推演链：思考模式分档需求 → 决策「四档强度（关闭/低/中/高），各标签独立记忆」→
 * 功能点：
 * - 默认强度 medium
 * - setStrength/getStrength 按 tabId 记忆，localStorage 持久化
 * - useActiveThinkingStrength：随 agentTabStore.activeTabId 变化自动返回该标签强度
 *   （切换对话标签 = 路由变化 → 输入框自动应用该标签强度）
 */

import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import {
  useThinkingModeStore,
  useActiveThinkingStrength,
  useExplicitThinkingStrength,
} from '@/stores/thinkingModeStore'
import { DEFAULT_THINKING_STRENGTH } from '@/types/thinkingMode'

describe('thinkingModeStore — 标签级思考强度', () => {
  beforeEach(() => {
    useThinkingModeStore.setState({ strengthByTabId: {} })
    useAgentTabStore.setState({
      activeTabId: null,
      tabs: [],
    })
    window.localStorage.clear()
  })

  it('未设置时默认 medium', () => {
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe(
      DEFAULT_THINKING_STRENGTH,
    )
  })

  it('setStrength 按标签记忆，getStrength 读回', () => {
    useThinkingModeStore.getState().setStrength('tab-a', 'high')
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe('high')
    // 其他标签不受影响
    expect(useThinkingModeStore.getState().getStrength('tab-b')).toBe(
      DEFAULT_THINKING_STRENGTH,
    )
  })

  it('setStrength 持久化到 localStorage，新实例可恢复', () => {
    useThinkingModeStore.getState().setStrength('tab-a', 'low')

    // 模拟重新加载：清空内存态后由 getStrength 惰性读 localStorage
    useThinkingModeStore.setState({ strengthByTabId: {} })
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe('low')
  })

  it('useActiveThinkingStrength 随 activeTabId 路由联动（切标签自动应用）', () => {
    useThinkingModeStore.getState().setStrength('main-s1', 'high')
    useThinkingModeStore.getState().setStrength('sub-1', 'low')

    useAgentTabStore.setState({ activeTabId: 'main-s1' })
    const { result, rerender } = renderHook(() => useActiveThinkingStrength())
    expect(result.current).toBe('high')

    // 切换标签（路由变化）→ 自动应用该标签强度
    act(() => {
      useAgentTabStore.setState({ activeTabId: 'sub-1' })
    })
    rerender()
    expect(result.current).toBe('low')

    // 无激活标签 → 默认
    act(() => {
      useAgentTabStore.setState({ activeTabId: null })
    })
    rerender()
    expect(result.current).toBe(DEFAULT_THINKING_STRENGTH)
  })

  it('切换标签后修改强度只影响当前标签，不影响其他标签', () => {
    useThinkingModeStore.getState().setStrength('main-s1', 'medium')
    useThinkingModeStore.getState().setStrength('sub-1', 'medium')

    useAgentTabStore.setState({ activeTabId: 'main-s1' })
    const { result, rerender } = renderHook(() => useActiveThinkingStrength())

    act(() => {
      useThinkingModeStore.getState().setStrength('main-s1', 'high')
    })
    rerender()
    expect(result.current).toBe('high')
    expect(useThinkingModeStore.getState().getStrength('sub-1')).toBe('medium')
  })

  it('getExplicitStrength：未设置过 → null（交由管道参数映射兜底）', () => {
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-new')).toBeNull()
  })

  it('getExplicitStrength：设置过后返回该值，localStorage 可恢复', () => {
    useThinkingModeStore.getState().setStrength('tab-a', 'low')
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-a')).toBe('low')

    // 清内存态模拟新会话 → localStorage 恢复显式值
    useThinkingModeStore.setState({ strengthByTabId: {} })
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-a')).toBe('low')
    // 未设置过的标签仍为 null
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-b')).toBeNull()
  })

  it('useExplicitThinkingStrength：随标签路由返回显式值或 null', () => {
    useThinkingModeStore.getState().setStrength('main-s1', 'high')

    useAgentTabStore.setState({ activeTabId: 'main-s1' })
    const { result, rerender } = renderHook(() => useExplicitThinkingStrength())
    expect(result.current).toBe('high')

    // 切到未设置过的标签 → null
    act(() => {
      useAgentTabStore.setState({ activeTabId: 'sub-new' })
    })
    rerender()
    expect(result.current).toBeNull()
  })
})
