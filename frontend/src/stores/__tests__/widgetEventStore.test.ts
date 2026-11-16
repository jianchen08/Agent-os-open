/**
 * widgetEventStore 单测（ADR §3.5' 内核统一配置驱动推送的前端消费层）
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import type { AdaptedWidgetEvent } from '@/services/websocket/MessageAdapter'

function ev(widgetId: string, value: number, sequence?: number): AdaptedWidgetEvent {
  return { widget_id: widgetId, event: 'snapshot', data: { value }, sequence }
}

describe('widgetEventStore', () => {
  beforeEach(() => {
    useWidgetEventStore.getState().clear()
  })

  it('dispatchWidgetEvent 按 widget_id 更新 latest', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    dispatchWidgetEvent(ev('w1', 2, 2))
    dispatchWidgetEvent(ev('w2', 99, 3))

    const state = useWidgetEventStore.getState()
    expect(state.latest['w1'].data.value).toBe(2)
    expect(state.latest['w2'].data.value).toBe(99)
  })

  it('无 widget_id 的事件被忽略', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ event: 'orphan', data: {} })
    const state = useWidgetEventStore.getState()
    expect(Object.keys(state.latest)).toHaveLength(0)
  })

  it('clear 清空所有', () => {
    const { dispatchWidgetEvent, clear } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    clear()
    const state = useWidgetEventStore.getState()
    expect(state.latest).toEqual({})
  })

  it('不同 widget_id 互不干扰', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    dispatchWidgetEvent(ev('w2', 2, 2))
    dispatchWidgetEvent(ev('w1', 3, 3))
    const state = useWidgetEventStore.getState()
    expect(state.latest['w1'].data.value).toBe(3)
    expect(state.latest['w2'].data.value).toBe(2)
  })
})
