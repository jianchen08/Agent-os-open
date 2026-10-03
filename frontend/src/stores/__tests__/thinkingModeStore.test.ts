/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：thinkingModeStore 标签级思考选择记忆 + 路由联动
 *
 * 推演链：思考模式选择需求 → 决策「选择值 = thinking_strength_params 配置的
 * 参数组 JSON 串（选项由 llm_service thinking-levels 端点下发），各标签独立
 * 记忆」→ 功能点：
 * - 未选择 = ''（消息不带思考参数）
 * - setStrength/getExplicitStrength 按 tabId 记忆（任意参数组串），localStorage 持久化
 */

import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import {
  useThinkingModeStore,
  useExplicitThinkingStrength,
} from '@/stores/thinkingModeStore'

const HIGH = '{"reasoning_effort":"max"}'
const OFF = '{"thinking":{"type":"disabled"}}'

describe('thinkingModeStore — 标签级思考选择', () => {
  beforeEach(() => {
    useThinkingModeStore.setState({ strengthByTabId: {} })
    useAgentTabStore.setState({
      activeTabId: null,
      tabs: [],
    })
    window.localStorage.clear()
  })

  it('未选择时为空串（消息不带思考参数）', () => {
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe('')
  })

  it('setStrength 按标签记忆，getStrength 读回，其他标签不受影响', () => {
    useThinkingModeStore.getState().setStrength('tab-a', HIGH)
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe(HIGH)
    expect(useThinkingModeStore.getState().getStrength('tab-b')).toBe('')
  })

  it('自定义参数组（关闭形态）记忆与恢复', () => {
    useThinkingModeStore.getState().setStrength('tab-a', OFF)
    expect(useThinkingModeStore.getState().getStrength('tab-a')).toBe(OFF)

    // 模拟重新加载：清空内存态后由 getStrength 惰性读 localStorage
    useThinkingModeStore.setState({ strengthByTabId: {} })
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-a')).toBe(OFF)
  })

  it('getExplicitStrength：未设置过 → null（显示值回落端点当前参数组）', () => {
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-new')).toBeNull()
  })

  it('getExplicitStrength：设置过后返回该值，localStorage 可恢复', () => {
    useThinkingModeStore.getState().setStrength('tab-a', HIGH)
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-a')).toBe(HIGH)

    // 清内存态模拟新会话 → localStorage 恢复显式值
    useThinkingModeStore.setState({ strengthByTabId: {} })
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-a')).toBe(HIGH)
    // 未设置过的标签仍为 null
    expect(useThinkingModeStore.getState().getExplicitStrength('tab-b')).toBeNull()
  })

  it('useExplicitThinkingStrength：随标签路由返回显式值或 null', () => {
    useThinkingModeStore.getState().setStrength('main-s1', HIGH)

    useAgentTabStore.setState({ activeTabId: 'main-s1' })
    const { result, rerender } = renderHook(() => useExplicitThinkingStrength())
    expect(result.current).toBe(HIGH)

    // 切到未设置过的标签 → null
    rerender()
    useAgentTabStore.setState({ activeTabId: 'sub-new' })
    rerender()
    expect(result.current).toBeNull()
  })
})
