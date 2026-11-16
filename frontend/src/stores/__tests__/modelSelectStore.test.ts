/** @feature FP-0.2.四 前端Schema模型选择记忆 | @ci: frontend-test */
/**
 * modelSelectStore 单测：标签级显式模型选择记忆 + 路由联动
 *
 * 断行为：
 * - 未选择 = ''（消息不带 model_id，后端按管线绑定/配置解析）
 * - setModel 按 tabId 记忆并持久化（key: model-select-{tabId}）；'' = 清除
 * - 内存态丢失（刷新/重进）后 getModel 从 localStorage 惰性恢复并回填内存
 * - localStorage 不可用（隐私模式等）→ 降级为纯内存态，不抛错
 * - useExplicitModelSelect 随激活标签返回选择；无激活标签 → ''
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useExplicitModelSelect, useModelSelectStore } from '@/stores/modelSelectStore'

const STORAGE_KEY = (tabId: string) => `model-select-${tabId}`

/** 临时把 localStorage 读取替换为抛错实现（模拟隐私模式/存储损坏） */
function withBrokenRead<T>(fn: () => T): T {
  const storage = window.localStorage
  const original = storage.getItem
  storage.getItem = () => {
    throw new Error('storage unavailable')
  }
  try {
    return fn()
  } finally {
    storage.getItem = original
  }
}

/** 临时把 localStorage 写入替换为抛错实现（模拟配额耗尽） */
function withBrokenWrite<T>(fn: () => T): T {
  const storage = window.localStorage
  const original = storage.setItem
  storage.setItem = () => {
    throw new Error('quota exceeded')
  }
  try {
    return fn()
  } finally {
    storage.setItem = original
  }
}

beforeEach(() => {
  useModelSelectStore.setState({ modelByTabId: {} })
  useAgentTabStore.setState({ activeTabId: null, tabs: [] })
  window.localStorage.clear()
})

describe('modelSelectStore — 标签级记忆与持久化', () => {
  it('未选择：getModel 返回空串（消息不带 model_id）', () => {
    expect(useModelSelectStore.getState().getModel('tab-a')).toBe('')
  })

  it('setModel 按标签记忆并落 localStorage，其他标签不受影响', () => {
    useModelSelectStore.getState().setModel('tab-a', 'deepseek-v3')
    useModelSelectStore.getState().setModel('tab-b', 'MiniMax-M3')

    expect(useModelSelectStore.getState().getModel('tab-a')).toBe('deepseek-v3')
    expect(useModelSelectStore.getState().getModel('tab-b')).toBe('MiniMax-M3')
    expect(window.localStorage.getItem(STORAGE_KEY('tab-a'))).toBe('deepseek-v3')
    expect(useModelSelectStore.getState().modelByTabId).toEqual({
      'tab-a': 'deepseek-v3',
      'tab-b': 'MiniMax-M3',
    })
  })

  it('setModel 空串 = 清除显式选择（内存键删除 + localStorage 移除）', () => {
    useModelSelectStore.getState().setModel('tab-a', 'deepseek-v3')
    useModelSelectStore.getState().setModel('tab-a', '')

    expect(useModelSelectStore.getState().getModel('tab-a')).toBe('')
    expect(window.localStorage.getItem(STORAGE_KEY('tab-a'))).toBeNull()
    expect(useModelSelectStore.getState().modelByTabId).not.toHaveProperty('tab-a')
  })

  it('内存态丢失后 getModel 从 localStorage 惰性恢复并回填内存（刷新/重进场景）', () => {
    window.localStorage.setItem(STORAGE_KEY('tab-a'), 'MiniMax-M3')
    expect(useModelSelectStore.getState().modelByTabId).toEqual({})

    expect(useModelSelectStore.getState().getModel('tab-a')).toBe('MiniMax-M3')
    // 回填后同标签再读不再依赖 storage（内存命中）
    expect(useModelSelectStore.getState().modelByTabId['tab-a']).toBe('MiniMax-M3')
  })

  it('localStorage 读不可用 → 视为未选择（不抛错）', () => {
    withBrokenRead(() => {
      expect(useModelSelectStore.getState().getModel('tab-a')).toBe('')
    })
    // 写入面不受影响（catch 只包读）
    window.localStorage.setItem(STORAGE_KEY('tab-b'), 'deepseek-v3')
    expect(useModelSelectStore.getState().getModel('tab-b')).toBe('deepseek-v3')
  })

  it('localStorage 写不可用 → 内存态仍生效（降级不抛错）', () => {
    withBrokenWrite(() => {
      useModelSelectStore.getState().setModel('tab-a', 'deepseek-v3')
    })

    expect(useModelSelectStore.getState().getModel('tab-a')).toBe('deepseek-v3')
    expect(window.localStorage.getItem(STORAGE_KEY('tab-a'))).toBeNull()
  })
})

describe('useExplicitModelSelect — 随路由联动', () => {
  it('无激活标签 → 空串', () => {
    useModelSelectStore.getState().setModel('tab-a', 'deepseek-v3')

    const { result } = renderHook(() => useExplicitModelSelect())
    expect(result.current).toBe('')
  })

  it('返回激活标签的显式选择，切标签随路由切换', () => {
    useModelSelectStore.getState().setModel('tab-a', 'deepseek-v3')
    useModelSelectStore.getState().setModel('tab-b', 'MiniMax-M3')
    useAgentTabStore.setState({ activeTabId: 'tab-a' })

    const { result, rerender } = renderHook(() => useExplicitModelSelect())
    expect(result.current).toBe('deepseek-v3')

    act(() => useAgentTabStore.setState({ activeTabId: 'tab-b' }))
    rerender()
    expect(result.current).toBe('MiniMax-M3')

    // 未选择过的标签 → 空串（不串味到其他标签的选择）
    act(() => useAgentTabStore.setState({ activeTabId: 'tab-c' }))
    rerender()
    expect(result.current).toBe('')
  })
})
