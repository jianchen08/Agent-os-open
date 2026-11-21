// @feature: FP-0.2.四 前端 Schema（widget 数据面读面） | @ci: frontend-test
/**
 * useWidgetEventValue / useWorkspaceTabs / usePipelineLiveUsage 测试 —
 * widget 数据面推送/布局读面（ADR 2026-10-10）。
 *
 * 真实 store 直种（非 mock），断言两组区分度输入：有 widgetId/无 widgetId、
 * 不同实例 id、多管道键。
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { usePipelineLiveUsage } from '@/hooks/usePipelineLiveUsage'
import { useWidgetEventValue } from '@/hooks/useWidgetEventValue'
import { useWorkspaceTabs } from '@/hooks/useWorkspaceTabs'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'

describe('useWidgetEventValue — widget_event 推送读面', () => {
  beforeEach(() => {
    useWidgetEventStore.getState().clear()
  })

  it('按 widgetId 取最新事件；不同实例互不串台', () => {
    const { result: r1 } = renderHook(() => useWidgetEventValue('w1'))
    const { result: r2 } = renderHook(() => useWidgetEventValue('w2'))
    expect(r1.current).toBeUndefined()

    act(() => {
      useWidgetEventStore.getState().dispatchWidgetEvent({
        widget_id: 'w1',
        event: 'value_changed',
        data: { value: 42 },
      })
    })
    expect(r1.current?.event).toBe('value_changed')
    expect(r1.current?.data).toEqual({ value: 42 })
    expect(r2.current).toBeUndefined()

    act(() => {
      useWidgetEventStore.getState().dispatchWidgetEvent({
        widget_id: 'w2',
        event: 'doc_loaded',
        data: {},
      })
    })
    expect(r2.current?.event).toBe('doc_loaded')
  })

  it('未传 widgetId → 不订阅任何实例（恒 undefined）', () => {
    const { result } = renderHook(() => useWidgetEventValue(undefined))
    act(() => {
      useWidgetEventStore.getState().dispatchWidgetEvent({
        widget_id: 'any',
        event: 'x',
        data: {},
      })
    })
    expect(result.current).toBeUndefined()
  })
})

describe('useWorkspaceTabs — 工作区页签读面', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [] })
  })

  it('页签集合开/合跟随（两组区分度状态）', () => {
    const { result } = renderHook(() => useWorkspaceTabs())
    expect(result.current).toEqual([])

    act(() => {
      useLayoutModeStore.setState({
        workspaceTabs: [
          {
            id: 'ws-tree-n1',
            title: '工作空间',
            icon: '📁',
            moduleId: '__dynamic__',
            component: 'file_tree',
            dataSource: 'workspace://n1',
            isActive: true,
            isPinned: false,
          },
        ],
      })
    })
    expect(result.current.map((t) => t.id)).toEqual(['ws-tree-n1'])

    act(() => {
      useLayoutModeStore.setState({
        workspaceTabs: [
          {
            id: 'ws-tree-n1',
            title: '工作空间',
            icon: '📁',
            moduleId: '__dynamic__',
            component: 'file_tree',
            dataSource: 'workspace://n1',
            isActive: true,
            isPinned: false,
          },
          {
            id: 'ws-tree-n2',
            title: '另一个',
            icon: '📁',
            moduleId: '__dynamic__',
            component: 'file_tree',
            dataSource: 'workspace://n2',
            isActive: false,
            isPinned: false,
          },
        ],
      })
    })
    expect(result.current.map((t) => t.id)).toEqual(['ws-tree-n1', 'ws-tree-n2'])
  })
})

describe('usePipelineLiveUsage — 管道实时用量读面', () => {
  beforeEach(() => {
    useContextUsageStore.setState({ usageByPipeline: {} })
  })

  it('按管道键取用量；多管道独立维护', () => {
    const { result } = renderHook(() => usePipelineLiveUsage())
    expect(result.current).toEqual({})

    act(() => {
      useContextUsageStore.getState().updateUsage('pipe-a', {
        prompt_tokens: 100,
        completion_tokens: 20,
        total_tokens: 120,
      })
    })
    expect(result.current['pipe-a']?.promptTokens).toBe(100)

    act(() => {
      useContextUsageStore.getState().updateUsage('pipe-b', {
        prompt_tokens: 7,
        completion_tokens: 3,
        total_tokens: 10,
      })
    })
    expect(Object.keys(result.current).sort()).toEqual(['pipe-a', 'pipe-b'])
    expect(result.current['pipe-b']?.totalTokens).toBe(10)
  })
})
